use arcturus_contracts::{ArtifactUploadRequest, ServiceReleaseEnvelope};

#[test]
fn parses_artifact_upload_fixture() {
    let raw = include_str!("../../../fixtures/artifact-upload-request.json");
    let request: ArtifactUploadRequest = serde_json::from_str(raw).expect("fixture parses");
    request.validate().expect("fixture validates");
    assert_eq!(request.service.as_str(), "stellar-project");
    assert_eq!(request.components.len(), 2);
}

#[test]
fn parses_service_release_envelope_fixture() {
    let raw = include_str!("../../../fixtures/service-release-v2.json");
    let release: ServiceReleaseEnvelope = serde_json::from_str(raw).expect("fixture parses");
    release.validate().expect("fixture validates");
    assert_eq!(release.metadata.name.as_str(), "stellar-project");
    assert!(release.spec.get("components").is_some());
}
