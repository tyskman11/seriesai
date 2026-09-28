from seriesai.services.age import chronological_age
def test_age_handles_birthday_boundary():
    assert chronological_age("2000-09-30","2026-09-29")==25
    assert chronological_age("2000-09-30","2026-09-30")==26
