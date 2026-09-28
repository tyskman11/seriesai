from seriesai.services.character_profiles import CharacterProfileOutput


def test_profile_schema_rejects_trait_values_outside_range():
    profile = CharacterProfileOutput(summary_de="Kurz", core_traits={"curiosity": 0.7}, confidence=0.4)
    assert profile.core_traits["curiosity"] == 0.7
