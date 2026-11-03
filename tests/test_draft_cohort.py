"""Synthetic captured-cohort regressions; never historical sports evidence."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from features.draft_replay import replay_draft

EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/draft_snapshot.synthetic.json'
STAMP = '2026-10-30T23:57:24.342957+00:00'


def fixture():
    data = json.loads(EXAMPLE.read_text())
    data['cutoff_at'] = data['draft_started_at'] = None
    data['rules'] = data['keepers'] = None
    data['player_pool']['complete'] = False
    data['player_pool']['observed_at'] = None
    for p in data['player_pool']['players']:
        p['eligible_positions'] = None
    data['rankings'].update(source='synthetic-provider', observed_at=STAMP, scoring_version=None)
    for i, e in enumerate(data['rankings']['entries']):
        e.update(source='synthetic-provider', observed_at=STAMP, scoring_version=None,
                 value=[10,30,30,0,9999,950,950,None,0][i])
    data['picks']['events'] = data['picks']['events'][:4]
    return data


def run(data):
    return replay_draft(data, assume_forecasts_pre_draft=True)


class CapturedDraftedCohortTests(unittest.TestCase):
    def test_real_gate_shape_yields_descriptive_numbers_only(self):
        data=fixture(); original=deepcopy(data); report=run(data)
        c=report['picks'][0]['captured_drafted_cohort_comparison']
        self.assertEqual(c['status'],'compared')
        self.assertEqual(c['highest_later_captured_value_minus_selected'],20)
        self.assertEqual(c['comparable_later_recorded_pick_count'],3)
        self.assertEqual([p['player_id'] for p in c['highest_later_captured_players']],['p002','p003'])
        self.assertFalse(c['availability_verified_by_this_comparison'])
        self.assertFalse(c['historical_scoring_verified_by_this_comparison'])
        self.assertEqual(report['captured_drafted_cohort_summary']['compared_pick_count'],3)
        self.assertEqual(report['status'],'partial')
        self.assertIsNone(report['cutoff_at']);self.assertIsNone(report['draft_started_at'])
        self.assertEqual(report['score_coverage']['p001']['provenance']['observed_at'],STAMP)
        for pick in report['picks']:
            self.assertIsNone(pick['available_player_ids']);self.assertIsNone(pick['selected_value'])
            self.assertIsNone(pick['selected_global_available_rank']);self.assertIsNone(pick['opportunity_gap_among_covered'])
        self.assertEqual(data,original)

    def test_undrafted_highest_player_is_never_a_comparator(self):
        report=run(fixture())
        c=report['picks'][0]['captured_drafted_cohort_comparison']
        self.assertEqual(c['highest_later_captured_value'],30)
        self.assertNotIn('p005',[p['player_id'] for p in c['highest_later_captured_players']])

    def test_earlier_selections_do_not_leak_and_ties_do_not_mutate(self):
        report=run(fixture()); first,second,third,last=[p['captured_drafted_cohort_comparison'] for p in report['picks']]
        self.assertEqual([p['player_id'] for p in second['highest_later_captured_players']],['p003'])
        self.assertEqual(second['highest_later_captured_value_minus_selected'],0)
        self.assertEqual(third['highest_later_captured_value_minus_selected'],-30)
        self.assertEqual(last['status'],'no_later_recorded_picks')
        self.assertIsNone(last['highest_later_captured_value_minus_selected'])

    def test_missing_is_excluded_while_zero_and_negative_values_remain(self):
        d=fixture(); d['rankings']['entries'][1]['value']=None;d['rankings']['entries'][2]['value']=-5
        r=run(d)
        self.assertEqual(r['picks'][0]['captured_drafted_cohort_comparison']['highest_later_captured_value'],0)
        c=r['picks'][1]['captured_drafted_cohort_comparison']
        self.assertEqual(c['status'],'excluded_projection');self.assertIsNone(c['highest_later_captured_value_minus_selected'])
        self.assertEqual(r['picks'][2]['captured_drafted_cohort_comparison']['highest_later_captured_value_minus_selected'],5)

    def test_wrong_league_season_horizon_and_missing_source_time_never_compare(self):
        for key,value in [('league_id','wrong'),('season_year',2027),('horizon','rest_of_season'),('source',None),('observed_at',None)]:
            with self.subTest(key=key):
                d=fixture();d['rankings']['entries'][1][key]=value;r=run(d)
                self.assertEqual(r['picks'][1]['captured_drafted_cohort_comparison']['status'],'excluded_projection')
                self.assertEqual(r['picks'][0]['captured_drafted_cohort_comparison']['comparable_later_recorded_pick_count'],2)

    def test_different_capture_source_or_declared_scoring_version_stays_separate(self):
        for key,value in [('observed_at','2026-10-31T00:00:00Z'),('source','different-provider'),('scoring_version','different-version')]:
            with self.subTest(key=key):
                d=fixture();d['rankings']['entries'][1][key]=value;r=run(d)
                c=r['picks'][1]['captured_drafted_cohort_comparison']
                self.assertEqual(c['status'],'no_comparable_later_recorded_picks')
                self.assertIsNone(c['highest_later_captured_value_minus_selected'])
                self.assertEqual(r['picks'][0]['captured_drafted_cohort_comparison']['comparable_later_recorded_pick_count'],2)

    def test_equivalent_timestamp_offsets_share_one_capture(self):
        d=fixture();d['rankings']['entries'][1]['observed_at']='2026-10-30T19:57:24.342957-04:00'
        self.assertEqual(run(d)['picks'][0]['captured_drafted_cohort_comparison']['comparable_later_recorded_pick_count'],3)

    def test_missing_parent_scope_cannot_be_repaired_by_entries(self):
        d=fixture();d['rankings']['league_id']=None
        self.assertEqual(run(d)['captured_drafted_cohort_summary']['compared_pick_count'],0)

    def test_numeric_league_ids_normalize_consistently(self):
        d=fixture();d['league_id']=123;d['rankings']['league_id']=123
        for e in d['rankings']['entries']:e['league_id']=123
        self.assertEqual(run(d)['captured_drafted_cohort_summary']['compared_pick_count'],3)

    def test_incomplete_observed_log_does_not_claim_missing_picks(self):
        d=fixture();d['picks']['complete']=False;d['picks']['events'].pop(1)
        r=run(d);c=r['picks'][0]['captured_drafted_cohort_comparison']
        self.assertEqual(c['later_recorded_pick_count'],2)
        self.assertEqual(c['comparable_later_recorded_pick_count'],2)
        self.assertFalse(r['picks'][0]['availability_complete'])
        self.assertIn('picks.incomplete',r['limitations'])

    def test_strict_default_does_not_enable_cohort_mode(self):
        with self.assertRaises(ValueError):replay_draft(fixture())
        r=replay_draft(json.loads(EXAMPLE.read_text()))
        self.assertIsNone(r['captured_drafted_cohort_summary'])
        self.assertNotIn('captured_drafted_cohort_comparison',r['picks'][0])

    def test_missing_rankings_stays_explicit(self):
        d=fixture();d['rankings']=None;r=run(d)
        self.assertEqual(r['captured_drafted_cohort_summary']['observed_numeric_forecast_count'],0)
        self.assertIsNone(r['picks'][0]['captured_drafted_cohort_comparison'])

    def test_nonfinite_difference_is_rejected(self):
        d=fixture();d['rankings']['entries'][0]['value']=-1e308;d['rankings']['entries'][1]['value']=1e308
        with self.assertRaisesRegex(ValueError,'captured-cohort difference'):run(d)

    def test_reordered_input_has_same_cohort_results(self):
        d=fixture();a=run(d);d['picks']['events'].reverse();d['rankings']['entries'].reverse();b=run(d)
        self.assertEqual(a['picks'],b['picks'])


if __name__=='__main__':unittest.main()
