import unittest

from engine import performance_ranking as rank
from engine.performance_targets import LOWER_IS_BETTER, STATE_DEGRADING, STATE_NOT_CLASSIFIED, STATE_STABLE, INFORMATIONAL_ONLY
from engine.savings_verification_evidence import EVIDENCE_CONTEXT_MATCHED, EVIDENCE_INSUFFICIENT, EVIDENCE_LIMITED, EVIDENCE_STRONG

"""
The mandatory correction: evidence quality must act as a confidence
GATE/companion field, never a score component. Poor/missing evidence
must never itself increase attention_score.
"""


def _obs(**overrides):
    defaults = dict(
        target_key="Power_kW", direction=LOWER_IS_BETTER, percent_change=10.0, performance_state=STATE_STABLE,
        evidence_quality=EVIDENCE_CONTEXT_MATCHED, participates_in_degradation=True,
        consecutive_degrading_observations=0,
    )
    defaults.update(overrides)
    return defaults


class TestAttentionRankingSeparatesEvidenceFromDegradation(unittest.TestCase):
    def test_high_degradation_strong_evidence_ranks_with_high_confidence(self):
        entry = rank.compute_attention_entry(
            1, 1, "p01", "P01.UTILITY.CHL01", "chiller",
            [_obs(percent_change=40.0, performance_state=STATE_DEGRADING, evidence_quality=EVIDENCE_STRONG, consecutive_degrading_observations=4)],
            [], criticality=None,
        )
        self.assertEqual(entry.attention_state, rank.ATTENTION_STATE_RANKED)
        self.assertGreater(entry.attention_score, 0)
        self.assertEqual(entry.evidence_quality, EVIDENCE_STRONG)

    def test_high_degradation_limited_evidence_still_ranked_but_marked_limited(self):
        entry = rank.compute_attention_entry(
            1, 1, "p01", "P01.UTILITY.CHL01", "chiller",
            [_obs(percent_change=40.0, performance_state=STATE_DEGRADING, evidence_quality=EVIDENCE_LIMITED, consecutive_degrading_observations=4)],
            [], criticality=None,
        )
        self.assertEqual(entry.attention_state, rank.ATTENTION_STATE_RANKED)
        self.assertGreater(entry.attention_score, 0)
        self.assertEqual(entry.evidence_quality, EVIDENCE_LIMITED)  # visibly distinct from STRONG

    def test_insufficient_evidence_does_not_fabricate_a_score(self):
        entry = rank.compute_attention_entry(
            1, 1, "p01", "P01.UTILITY.CHL01", "chiller",
            [_obs(percent_change=None, performance_state="INSUFFICIENT_EVIDENCE", evidence_quality=EVIDENCE_INSUFFICIENT)],
            [], criticality=None,
        )
        self.assertEqual(entry.attention_state, rank.ATTENTION_STATE_INSUFFICIENT_EVIDENCE)
        self.assertIsNone(entry.attention_score)

    def test_missing_telemetry_never_makes_asset_appear_worse_than_a_confirmed_stable_one(self):
        insufficient = rank.compute_attention_entry(
            1, 1, "p01", "P01.A", "chiller", [_obs(percent_change=None, performance_state="INSUFFICIENT_EVIDENCE", evidence_quality=EVIDENCE_INSUFFICIENT)], [], None,
        )
        stable = rank.compute_attention_entry(
            2, 1, "p01", "P01.B", "chiller", [_obs(percent_change=1.0, performance_state=STATE_STABLE, evidence_quality=EVIDENCE_CONTEXT_MATCHED)], [], None,
        )
        self.assertIsNone(insufficient.attention_score)
        self.assertEqual(stable.attention_score, 0.0)
        # INSUFFICIENT is a DISTINCT state, never silently ranked below/above a confirmed-fine equipment as if comparable.
        self.assertNotEqual(insufficient.attention_state, stable.attention_state)

    def test_insufficient_dimension_excluded_but_other_usable_dimension_still_scores(self):
        entry = rank.compute_attention_entry(
            1, 1, "p01", "P01.UTILITY.CHL01", "chiller",
            [
                _obs(target_key="Power_kW", percent_change=None, performance_state="INSUFFICIENT_EVIDENCE", evidence_quality=EVIDENCE_INSUFFICIENT),
                _obs(target_key="cop", percent_change=25.0, performance_state=STATE_DEGRADING, evidence_quality=EVIDENCE_STRONG, direction="HIGHER_IS_BETTER"),
            ],
            [], criticality=None,
        )
        self.assertEqual(entry.attention_state, rank.ATTENTION_STATE_RANKED)
        self.assertEqual(entry.worst_dimension_target_key, "cop")
        self.assertEqual(entry.excluded_insufficient_dimension_count, 1)

    def test_informational_only_dimension_never_drives_the_score(self):
        entry = rank.compute_attention_entry(
            1, 1, "p01", "P01.UTILITY.AC01", "air_compressor",
            [_obs(target_key="Pressure", direction=INFORMATIONAL_ONLY, performance_state=STATE_NOT_CLASSIFIED, participates_in_degradation=False, evidence_quality=EVIDENCE_STRONG, percent_change=90.0)],
            [], criticality=None,
        )
        self.assertEqual(entry.attention_state, rank.ATTENTION_STATE_INSUFFICIENT_EVIDENCE)

    def test_criticality_contributes_explicitly(self):
        low_crit = rank.compute_attention_entry(1, 1, "p01", "A", "chiller", [_obs()], [], criticality="Low")
        high_crit = rank.compute_attention_entry(1, 1, "p01", "A", "chiller", [_obs()], [], criticality="Critical")
        self.assertGreater(high_crit.attention_score, low_crit.attention_score)

    def test_worsened_maintenance_increases_score_improved_does_not(self):
        worsened = rank.compute_attention_entry(1, 1, "p01", "A", "chiller", [_obs()], [{"effectiveness_result": "WORSENED"}], None)
        improved = rank.compute_attention_entry(1, 1, "p01", "A", "chiller", [_obs()], [{"effectiveness_result": "IMPROVED"}], None)
        self.assertGreater(worsened.attention_score, improved.attention_score)

    def test_component_breakdown_is_inspectable_not_opaque(self):
        entry = rank.compute_attention_entry(1, 1, "p01", "A", "chiller", [_obs(percent_change=40.0, performance_state=STATE_DEGRADING, consecutive_degrading_observations=3)], [], "High")
        self.assertEqual(
            round(entry.degradation_component + entry.persistence_component + entry.criticality_component + entry.maintenance_ineffectiveness_component, 1),
            entry.attention_score,
        )


if __name__ == "__main__":
    unittest.main()
