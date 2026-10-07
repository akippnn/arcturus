use std::env;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ArcturusPaths {
    pub config_dir: PathBuf,
    pub deployer_state_dir: PathBuf,
    pub fleet_state_dir: PathBuf,
    pub agent_state_dir: PathBuf,
    pub oci_auth_state_dir: PathBuf,
    pub oci_registry_state_dir: PathBuf,
    pub cache_dir: PathBuf,
    pub runtime_dir: Option<PathBuf>,
    pub systemd_dir: PathBuf,
    pub quadlet_dir: PathBuf,
    pub bin_dir: PathBuf,
    pub workload_root: PathBuf,
}

impl ArcturusPaths {
    pub fn from_environment() -> Result<Self, String> {
        Self::resolve(|name| env::var(name).ok())
    }

    pub fn resolve(get: impl Fn(&str) -> Option<String>) -> Result<Self, String> {
        let home = configured(&get, "HOME")
            .map(|value| absolute("HOME", PathBuf::from(value)))
            .transpose()?;
        let config_root = root(
            &get,
            "ARCTURUS_CONFIG_ROOT",
            "XDG_CONFIG_HOME",
            &home,
            ".config",
        )?;
        let data_root = root(
            &get,
            "ARCTURUS_DATA_ROOT",
            "XDG_DATA_HOME",
            &home,
            ".local/share",
        )?;
        let cache_root = root(
            &get,
            "ARCTURUS_CACHE_ROOT",
            "XDG_CACHE_HOME",
            &home,
            ".cache",
        )?;
        let runtime_root = configured(&get, "ARCTURUS_RUNTIME_ROOT")
            .or_else(|| configured(&get, "XDG_RUNTIME_DIR"))
            .map(|value| absolute("ARCTURUS_RUNTIME_ROOT", PathBuf::from(value)))
            .transpose()?
            .or_else(default_runtime_root);
        Ok(Self {
            config_dir: final_dir(&get, "ARCTURUS_CONFIG_DIR", config_root.join("arcturus"))?,
            deployer_state_dir: final_dir(
                &get,
                "ARCTURUS_STATE_DIR",
                data_root.join("arcturus-deployer"),
            )?,
            fleet_state_dir: final_dir(
                &get,
                "ARCTURUS_FLEET_STATE_DIR",
                data_root.join("arcturus-fleet"),
            )?,
            agent_state_dir: final_dir(
                &get,
                "ARCTURUS_AGENT_STATE_DIR",
                data_root.join("arcturus-agent"),
            )?,
            oci_auth_state_dir: final_dir(
                &get,
                "ARCTURUS_OCI_AUTH_STATE_DIR",
                data_root.join("arcturus-oci-auth"),
            )?,
            oci_registry_state_dir: final_dir(
                &get,
                "ARCTURUS_OCI_REGISTRY_STATE_DIR",
                data_root.join("arcturus-registry"),
            )?,
            cache_dir: final_dir(&get, "ARCTURUS_CACHE_DIR", cache_root.join("arcturus"))?,
            runtime_dir: get("ARCTURUS_RUNTIME_DIR")
                .map(|value| absolute("ARCTURUS_RUNTIME_DIR", PathBuf::from(value)))
                .transpose()?
                .or_else(|| runtime_root.map(|root| root.join("arcturus"))),
            systemd_dir: final_dir(
                &get,
                "ARCTURUS_SYSTEMD_DIR",
                config_root.join("systemd/user"),
            )?,
            quadlet_dir: final_dir(
                &get,
                "ARCTURUS_QUADLET_DIR",
                config_root.join("containers/systemd/arcturus"),
            )?,
            bin_dir: final_dir(
                &get,
                "ARCTURUS_BIN_DIR",
                optional_default(&get, "ARCTURUS_BIN_DIR", &home, ".local/bin")?,
            )?,
            workload_root: final_dir(
                &get,
                "ARCTURUS_WORKLOAD_ROOT",
                optional_default(&get, "ARCTURUS_WORKLOAD_ROOT", &home, "stacks")?,
            )?,
        })
    }

    pub fn fleet_database(&self) -> PathBuf {
        self.fleet_state_dir.join("state.sqlite3")
    }

    pub fn agent_database(&self) -> PathBuf {
        self.agent_state_dir.join("state.sqlite3")
    }

    pub fn fleet_operator_tokens(&self) -> PathBuf {
        self.config_dir.join("fleet-tokens.json")
    }

    pub fn agent_config(&self) -> PathBuf {
        self.config_dir.join("agent.toml")
    }

    pub fn lifecycle_tokens(&self) -> PathBuf {
        self.config_dir.join("tokens.json")
    }

    pub fn oci_grant_database(&self) -> PathBuf {
        self.oci_auth_state_dir.join("grants.sqlite3")
    }

    pub fn oci_jwks(&self) -> PathBuf {
        self.oci_auth_state_dir.join("jwks.json")
    }

    pub fn require_runtime_dir(&self) -> Result<&Path, String> {
        self.runtime_dir
            .as_deref()
            .ok_or_else(|| "XDG_RUNTIME_DIR or ARCTURUS_RUNTIME_ROOT is required".to_owned())
    }
}

/// Compatibility alias for the DIST-001 name. New code should use
/// `ArcturusPaths` because the resolver now owns host-local and OCI paths too.
pub type FleetPaths = ArcturusPaths;

fn final_dir(
    get: &impl Fn(&str) -> Option<String>,
    name: &str,
    fallback: PathBuf,
) -> Result<PathBuf, String> {
    absolute(
        name,
        configured(get, name).map(PathBuf::from).unwrap_or(fallback),
    )
}

fn optional_default(
    get: &impl Fn(&str) -> Option<String>,
    name: &str,
    home: &Option<PathBuf>,
    fallback: &str,
) -> Result<PathBuf, String> {
    if let Some(value) = configured(get, name) {
        return absolute(name, PathBuf::from(value));
    }
    home.as_ref()
        .map(|value| value.join(fallback))
        .ok_or_else(|| format!("{name} or HOME is required"))
}

fn configured(get: &impl Fn(&str) -> Option<String>, name: &str) -> Option<String> {
    get(name).filter(|value| !value.is_empty())
}

fn absolute(name: &str, path: PathBuf) -> Result<PathBuf, String> {
    if !path.is_absolute() {
        return Err(format!(
            "{name} must be an absolute path: {}",
            path.display()
        ));
    }
    if path
        .to_string_lossy()
        .chars()
        .any(|character| matches!(character, '\n' | '\r' | '\0'))
    {
        return Err(format!("{name} contains a forbidden control character"));
    }
    Ok(path)
}

fn root(
    get: &impl Fn(&str) -> Option<String>,
    override_name: &str,
    xdg_name: &str,
    home: &Option<PathBuf>,
    fallback: &str,
) -> Result<PathBuf, String> {
    if let Some(value) = configured(get, override_name).or_else(|| configured(get, xdg_name)) {
        return absolute(override_name, PathBuf::from(value));
    }
    home.as_ref()
        .map(|path| path.join(Path::new(fallback)))
        .ok_or_else(|| format!("{override_name}, {xdg_name}, or HOME is required"))
}

#[cfg(unix)]
fn default_runtime_root() -> Option<PathBuf> {
    // SAFETY: geteuid has no preconditions and does not dereference memory.
    let uid = unsafe { libc::geteuid() };
    Some(PathBuf::from(format!("/run/user/{uid}")))
}

#[cfg(not(unix))]
fn default_runtime_root() -> Option<PathBuf> {
    None
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use super::*;

    #[test]
    fn canonical_fhs_fixture_matches_rust_resolver() {
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("../../../fixtures/paths/fhs-layout.json")).unwrap();
        let environment = fixture["environment"].as_object().unwrap();
        let paths = ArcturusPaths::resolve(|key| {
            environment
                .get(key)
                .and_then(serde_json::Value::as_str)
                .map(ToOwned::to_owned)
        })
        .unwrap();
        let expected = fixture["expected"].as_object().unwrap();
        let actual = BTreeMap::from([
            ("config_dir", paths.config_dir),
            ("deployer_state_dir", paths.deployer_state_dir),
            ("fleet_state_dir", paths.fleet_state_dir),
            ("agent_state_dir", paths.agent_state_dir),
            ("oci_auth_state_dir", paths.oci_auth_state_dir),
            ("oci_registry_state_dir", paths.oci_registry_state_dir),
            ("cache_dir", paths.cache_dir),
            ("runtime_dir", paths.runtime_dir.unwrap()),
            ("systemd_dir", paths.systemd_dir),
            ("quadlet_dir", paths.quadlet_dir),
            ("bin_dir", paths.bin_dir),
            ("workload_root", paths.workload_root),
        ]);
        for (key, value) in actual {
            assert_eq!(
                value,
                PathBuf::from(expected[key].as_str().unwrap()),
                "{key}"
            );
        }
    }

    #[test]
    fn resolves_rootless_xdg_defaults() {
        let values = BTreeMap::from([("HOME", "/home/app"), ("XDG_RUNTIME_DIR", "/run/user/1000")]);
        let paths = ArcturusPaths::resolve(|key| values.get(key).map(ToString::to_string)).unwrap();
        assert_eq!(
            paths.fleet_database(),
            PathBuf::from("/home/app/.local/share/arcturus-fleet/state.sqlite3")
        );
        assert_eq!(
            paths.agent_config(),
            PathBuf::from("/home/app/.config/arcturus/agent.toml")
        );
        assert_eq!(
            paths.runtime_dir,
            Some(PathBuf::from("/run/user/1000/arcturus"))
        );
    }

    #[test]
    fn explicit_roots_support_fhs_layouts() {
        let values = BTreeMap::from([
            ("ARCTURUS_CONFIG_ROOT", "/etc"),
            ("ARCTURUS_DATA_ROOT", "/var/lib"),
            ("ARCTURUS_CACHE_ROOT", "/var/cache"),
            ("ARCTURUS_RUNTIME_ROOT", "/run"),
            ("ARCTURUS_BIN_DIR", "/usr/bin"),
            ("ARCTURUS_WORKLOAD_ROOT", "/srv/arcturus-workloads"),
        ]);
        let paths = ArcturusPaths::resolve(|key| values.get(key).map(ToString::to_string)).unwrap();
        assert_eq!(paths.config_dir, PathBuf::from("/etc/arcturus"));
        assert_eq!(
            paths.fleet_database(),
            PathBuf::from("/var/lib/arcturus-fleet/state.sqlite3")
        );
        assert_eq!(paths.runtime_dir, Some(PathBuf::from("/run/arcturus")));
        assert_eq!(paths.cache_dir, PathBuf::from("/var/cache/arcturus"));
        assert_eq!(paths.systemd_dir, PathBuf::from("/etc/systemd/user"));
    }

    #[test]
    fn runtime_path_falls_back_to_the_effective_users_runtime_root() {
        let values = BTreeMap::from([("HOME", "/home/app")]);
        let paths = ArcturusPaths::resolve(|key| values.get(key).map(ToString::to_string)).unwrap();
        #[cfg(unix)]
        assert_eq!(
            paths.runtime_dir,
            Some(PathBuf::from(format!("/run/user/{}/arcturus", unsafe {
                libc::geteuid()
            })))
        );
    }

    #[test]
    fn rejects_control_characters_in_paths() {
        let values = BTreeMap::from([
            ("HOME", "/home/app"),
            ("ARCTURUS_CONFIG_ROOT", "/etc/arcturus\ninvalid"),
        ]);
        let error =
            ArcturusPaths::resolve(|key| values.get(key).map(ToString::to_string)).unwrap_err();
        assert_eq!(
            error,
            "ARCTURUS_CONFIG_ROOT contains a forbidden control character"
        );
    }

    #[test]
    fn empty_xdg_and_final_values_are_unset() {
        let values = BTreeMap::from([
            ("HOME", "/home/app"),
            ("XDG_CONFIG_HOME", ""),
            ("ARCTURUS_STATE_DIR", ""),
        ]);
        let paths = ArcturusPaths::resolve(|key| values.get(key).map(ToString::to_string)).unwrap();
        assert_eq!(
            paths.config_dir,
            PathBuf::from("/home/app/.config/arcturus")
        );
        assert_eq!(
            paths.deployer_state_dir,
            PathBuf::from("/home/app/.local/share/arcturus-deployer")
        );
    }
}
