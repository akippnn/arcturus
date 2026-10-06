use arcturus_contracts::{
    ArtifactUploadRequest, ResourceRecord, ServiceReleaseEnvelope, WorkloadIntent,
};

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

#[test]
fn parses_fleet_resource_and_workload_fixtures() {
    let resource: ResourceRecord = serde_json::from_str(include_str!(
        "../../../fixtures/fleet/imported-redis-resource.json"
    ))
    .expect("resource fixture parses");
    assert_eq!(resource.protocol, "resp");
    let intent: WorkloadIntent =
        serde_json::from_str(include_str!("../../../fixtures/fleet/workload-intent.json"))
            .expect("workload fixture parses");
    assert_eq!(intent.workload_name.as_str(), "dist-redis-client");
    assert_eq!(intent.resource_bindings.len(), 1);
}
