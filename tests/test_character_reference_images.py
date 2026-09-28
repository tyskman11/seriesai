from seriesai.services.character_reference_images import CharacterReferenceImageService


def test_evidence_episode_ids_are_unique_and_stable():
    evidence = ["Wiederkehrend: demo_s01e02, demo_s01e03", "demo_s01e02"]
    assert CharacterReferenceImageService.evidence_episode_ids(evidence) == ["demo_s01e02", "demo_s01e03"]
