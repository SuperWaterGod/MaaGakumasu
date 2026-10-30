"""培育报告离线回归：审计异常、分母与证据关联，使用合成素材。"""
import sys
import json
import hashlib
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'agent')]
from research.produce.analyze_choices import POLICY_FILES, build_baseline
from research.produce.produce_reporting import build_corpus, timeline_summary
from research.produce.report_produce_session import build_report


class ReportingTest(unittest.TestCase):
    def setUp(self):
        temporary_root = ROOT.parent / 'MaaGakumasuLab/debug/research/produce/tests'
        temporary_root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='reporting_', dir=temporary_root)
        self.base = Path(temporary.name).resolve()
        assert self.base.parent == temporary_root.resolve()
        self.addCleanup(temporary.cleanup)
        self.capture = self.base / 'produce_sessions/s1/data'
        self.directory = self.base / 'produce_sessions/s1'
        self.directory.mkdir(parents=True)
        self.capture.mkdir()
        self.bindings = {'scenario': 'NIA', 'difficulty': ['PRO'], 'configured_idol': {'idol_name': '偶像', 'song_name': '歌曲'}, 'deck': {}}
        self.session = {'session_id': 's1', 'schema_version': 4, 'start_source': 'ProduceEntryNIA', 'bindings': self.bindings, 'segments': [],
                        'started_at': '2000-01-01T00:00:00+00:00', 'gameplay_started_at': '2000-01-01T00:00:00+00:00',
                        'start_observed': True, 'settlement_observed': True, 'status': 'ended',
                        'stop_reason': 'returned_home', 'ended_at': '2000-01-01T00:01:00+00:00'}
        self.events = []
        self.results = {}
        self.add('produce_session_start', bindings=self.bindings)
        self.add('produce_gameplay_entry', fresh_start=True)
        self.add('produce_event_choice', selected={'name': '休息'}, candidates=[{'休息': [0, 0, 1, 1]}], frame=True)
        self.add('produce_event_input_result', decision_id='run:3', framework_action_succeeded=True)
        event = self.add('settlement_observation', frame=True)
        self.results[event['event_id']] = {'source_frame_sha256': event['frame']['sha256'],
            'settlement': {k: {'value': v} for k, v in {'score': 12000, 'rank': 'S', 'Vo': 800, 'Da': 900, 'Vi': 1000}.items()}}
        self.add('produce_session_end', terminal=True, reason='returned_home')

    def add(self, kind, **data):
        seq = len(self.events) + 1
        event = {'kind': kind, 'seq': seq, 'event_id': f'run:{seq}', 'run_id': 'run', 'segment_id': 'run', 'session_id': 's1',
                 'time': f'2000-01-01T00:00:{seq:02d}+00:00', **data}
        if data.get('frame') is True:
            image = kind.encode()
            digest = hashlib.sha256(image).hexdigest()
            path = self.capture / 'run/frames' / (digest + '.png')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(image)
            event['frame'] = {'path': f'frames/{digest}.png', 'sha256': digest}
        self.events.append(event)
        return event

    def write(self, second=False):
        run = self.capture / 'run'
        run.mkdir(parents=True, exist_ok=True)
        manifest = {'run_id': 'run', 'status': 'closed', 'events_written': len(self.events), 'events_dropped': 0,
                    'started_at': self.session['started_at'], 'ended_at': self.session['ended_at'],
                    'metadata': {'session_id': 's1', 'bindings': self.bindings, 'entry': 'ProduceEntryNIA', 'version_files': {'agent.py': 'hash'}}}
        (run / 'manifest.json').write_text(json.dumps(manifest), encoding='utf8')
        (run / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in self.events) + '\n', encoding='utf8')
        derived = run / 'analysis'
        derived.mkdir(parents=True, exist_ok=True)
        (derived / 'status.json').write_text(json.dumps({'status': 'completed', 'analysis_id': 'old'}), encoding='utf8')
        links = [{'event_id': event_id, 'observation': result} for event_id, result in self.results.items()]
        (derived / 'results.jsonl').write_text('\n'.join(json.dumps(link) for link in links) + '\n', encoding='utf8')
        self.session['segments'] = [{'run_id': 'run', 'relative_path': 'data/run'}] + ([{'run_id': 'run', 'relative_path': 'data/run'}] if second else [])
        (self.directory / 'data/session.json').write_text(json.dumps(self.session), encoding='utf8')
        return build_report(self.directory)

    def codes(self, report):
        return {item['code'] for item in report['integrity']['issues']}

    def test_normal_and_trace(self):
        report = self.write()
        self.assertTrue(report['complete'])
        self.assertEqual(report['integrity']['state'], 'normal_completion')
        choice = next(r for r in report['timeline'] if r['kind'] == 'produce_event_choice')
        self.assertEqual(choice['evidence']['line'], 3)
        self.assertTrue(Path(choice['evidence']['frame_path']).exists())
        self.assertIsNone(report['uncollected']['shop_cost']['value'])

    def test_sp_is_candidate_attribute(self):
        summary = timeline_summary({'kind': 'produce_event_choice', 'detail': {
            'candidates': [{'Vo': [190, 1000, 1, 1], 'SP': False}, {'Da': [360, 1000, 1, 1], 'SP': True}],
            'selected': {'name': 'Da_SP'},
        }})
        self.assertEqual(summary, '候选：Vo、Da_SP；选择：Da_SP')

    def _reward_baseline_fixture(self):
        self.events[-1]['kind'] = 'analysis_started'
        page = self.add('reward_page_start', frame=True, selected_candidate_id='a',
                        layout={'candidates': [{'candidate_id': 'a'}, {'candidate_id': 'b'}]})
        previews = []
        for candidate in ('a', 'a', 'b'):
            preview = self.add('reward_candidate_preview', frame=True, reward_page_id=page['event_id'], candidate={'candidate_id': candidate})
            previews.append(preview['event_id'])
            self.results[preview['event_id']] = {'identity_verified': True, 'detail_verified': True,
                'item_kind': 'skill_card', 'observed_name': candidate, 'identity': {'catalog_id': candidate, 'database_name': candidate}}
        self.add('reward_selection_restored', reward_page_id=page['event_id'], selected_candidate_id='a', verified=True)
        self.add('reward_page_end', reward_page_id=page['event_id'], candidate_previews_complete=True)
        self.add('reward_receive_input', reward_page_id=page['event_id'], selected_candidate_id='a', framework_action_succeeded=True)
        self.add('produce_session_end', terminal=True, reason='returned_home')
        return previews

    def test_baseline_preview_retry_not_an_extra_opportunity(self):
        self._reward_baseline_fixture()
        result = build_baseline([self.write()], self.base / 'baseline')
        self.assertEqual(result['summary']['reward_candidates'], 2)
        items = {item['catalog_id']: item for item in result['summary']['reward_opportunities']}
        self.assertEqual(items['a']['offered_pages'], 1)
        self.assertEqual(items['a']['selection_when_offered']['success'], 1)
        self.assertEqual(items['b']['selection_when_offered']['total'], 1)

    def test_baseline_conflicting_identity_remains_unknown(self):
        previews = self._reward_baseline_fixture()
        self.results[previews[1]]['identity']['catalog_id'] = 'conflicting'
        result = build_baseline([self.write()], self.base / 'baseline')
        self.assertEqual(result['summary']['unknown_reward_candidates'], 1)
        self.assertEqual(result['summary']['unknown_selected_reward_identities'], 1)
        self.assertIsNone(result['sessions'][0]['rewards'][0]['candidates'][0]['catalog_id'])
        self.assertIsNone(result['summary']['uncollected']['shop_cost']['value'])
        self.assertEqual(result['readiness'][1]['status'], '部分可用')

    def test_baseline_unverified_selection_does_not_mean_not_selected(self):
        self._reward_baseline_fixture()
        next(e for e in self.events if e['kind'] == 'reward_receive_input')['framework_action_succeeded'] = False
        result = build_baseline([self.write()], self.base / 'baseline')
        self.assertEqual(result['summary']['verified_reward_selection_inputs'], 0)
        for item in result['summary']['reward_opportunities']:
            self.assertEqual(item['offered_pages'], 1)
            self.assertIsNone(item['selection_when_offered']['rate'])

    def test_baseline_same_catalog_in_two_slots_counts_one_page(self):
        previews = self._reward_baseline_fixture()
        self.results[previews[-1]]['identity']['catalog_id'] = 'a'
        result = build_baseline([self.write()], self.base / 'baseline')
        self.assertEqual(result['summary']['reward_candidates'], 2)
        items = result['summary']['reward_opportunities']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['offered_pages'], 1)
        self.assertEqual(items[0]['selection_when_offered']['success'], 1)

    def test_baseline_unmatched_event_selection_stays_unknown(self):
        self.events[2]['selected']['name'] = '未匹配的选择'
        result = build_baseline([self.write()], self.base / 'baseline')
        self.assertEqual(result['summary']['unknown_event_choices'], 1)
        self.assertIsNone(result['summary']['event_opportunities'][0]['selection_when_available']['rate'])

    def test_baseline_sp_metadata_is_not_an_event(self):
        self.events[2].update(candidates=[{'Vo': [190, 1000, 1, 1], 'SP': False}, {'Da': [360, 1000, 1, 1], 'SP': True}],
                              selected={'name': 'Da_SP', 'box': [360, 1000, 1, 1]})
        result = build_baseline([self.write()], self.base / 'baseline')
        self.assertEqual(result['summary']['event_selection_counts'], {'Da_SP': 1})
        self.assertEqual({v['name'] for v in result['summary']['event_opportunities']}, {'Vo', 'Da_SP'})

    def test_baseline_card_song_and_code_versions_stay_separate(self):
        first = self.write()
        second = deepcopy(first)
        second['session']['session_id'] = 's2'
        second['session']['bindings']['configured_idol']['song_name'] = '另一首歌曲'
        second['segments'][0]['run_id'] = 'run2'
        third = deepcopy(first)
        third['session']['session_id'] = 's3'
        third['code_versions'] = ['another_version']
        third['segments'][0]['run_id'] = 'run3'
        result = build_baseline([first, second, third, first], self.base / 'baseline')
        self.assertEqual(len(result['groups']), 3)
        self.assertEqual(result['summary']['sessions'], 3)
        self.assertEqual(result['excluded'][0]['reason'], 'duplicate_session')
        self.assertEqual(result['matched_configuration_observations'], [])

    def test_baseline_incomplete_or_shared_rounds_excluded(self):
        first = self.write()
        incomplete = deepcopy(first)
        incomplete['complete'] = False
        result = build_baseline([incomplete], self.base / 'baseline_incomplete')
        self.assertEqual(result['summary']['sessions'], 0)
        second = deepcopy(first)
        second['session']['session_id'] = 's2'
        result = build_baseline([first, second], self.base / 'baseline_shared')
        self.assertEqual(result['summary']['sessions'], 0)
        self.assertEqual({e['reason'] for e in result['excluded']}, {'shared_segment'})

    def test_baseline_same_policy_is_not_causal_evidence(self):
        self.bindings['node_settings'] = {'ProduceChooseNIAEventFlag': {'first': 'Vi'}}
        first = self.write()
        first['segments'][0]['manifest']['metadata']['version_files'] = {name: 'same' for name in POLICY_FILES}
        first['evaluation_check']['calculated'] = {'parameter_rating': 100, 'vote_rating': 200}
        second = deepcopy(first)
        second['session']['session_id'] = 's2'
        second['segments'][0]['run_id'] = 'run2'
        second['code_versions'] = ['another_capture_version']
        second['timing']['started_at'] = '2000-01-01T01:00:00+00:00'
        second['final_settlement']['score']['value'] += 100
        second['evaluation_check']['calculated'] = {'parameter_rating': 180, 'vote_rating': 220}
        result = build_baseline([first, second], self.base / 'baseline')
        pair = result['matched_configuration_observations'][0]
        self.assertEqual(pair['score_delta'], 100)
        self.assertEqual(pair['formula_component_delta']['parameter_rating'], 80)
        self.assertEqual(pair['formula_component_delta']['vote_rating'], 20)
        self.assertEqual(result['readiness'][4]['status'], '证据不足')

    def test_duplicate_event_and_segment(self):
        self.events.append(deepcopy(self.events[2]))
        report = self.write(second=True)
        self.assertIn('duplicate_event', self.codes(report))
        self.assertIn('duplicate_segment', self.codes(report))
        self.assertEqual(report['event_counts']['produce_event_choice'], 1)
        self.assertFalse(report['complete'])

    def test_foreign_event(self):
        self.events[2]['session_id'] = 'other'
        report = self.write()
        self.assertIn('foreign_event', self.codes(report))
        self.assertNotIn('run:3', report['produce_events'])

    def test_reward_duplicate_receive_and_unique_denominator(self):
        page = self.add('reward_page_start', frame=True, selected_candidate_id='a', confirmation_id='run:3',
                        layout={'candidates': [{'candidate_id': 'a'}, {'candidate_id': 'b'}]})
        for candidate in ('a', 'a', 'b'):
            preview = self.add('reward_candidate_preview', frame=True, reward_page_id=page['event_id'], candidate={'candidate_id': candidate})
            self.results[preview['event_id']] = {'identity_verified': True, 'detail_verified': True}
        self.add('reward_selection_restored', reward_page_id=page['event_id'], selected_candidate_id='a', verified=True)
        self.add('reward_page_end', reward_page_id=page['event_id'], candidate_previews_complete=True)
        for _ in range(2):
            self.add('reward_receive_input', reward_page_id=page['event_id'], selected_candidate_id='a')
        report = self.write()
        self.assertIn('duplicate_reward_receive', self.codes(report))
        self.assertEqual(report['recognition']['reward_identity'], {'success': 2, 'total': 2, 'rate': 1})

    def test_missing_and_conflicting_settlement(self):
        self.results['run:5']['settlement']['score']['value'] = None
        report = self.write()
        self.assertEqual(report['integrity']['settlement_missing_reasons']['score'], 'not_readable_in_saved_frames')
        self.assertIsNone(report['final_settlement']['score']['value'])
        event = self.add('settlement_observation', frame=True)
        self.results['run:5']['settlement']['score']['value'] = 12000
        self.results[event['event_id']] = {'settlement': {'score': {'value': 13000}}}
        report = self.write()
        self.assertIn('settlement_conflicting_readings', self.codes(report))
        self.assertIsNone(report['final_settlement']['score']['value'])

    def test_frame_hash_and_stale_analysis(self):
        self.results['run:5']['source_frame_sha256'] = 'other'
        report = self.write()
        self.assertIn('analysis_frame_mismatch', self.codes(report))
        self.assertIsNone(report['final_settlement']['score']['value'])
        Path(report['timeline'][2]['evidence']['frame_path']).write_bytes(b'changed')
        report = self.write()
        self.assertIn('frame_integrity', self.codes(report))

    def test_preparation_excluded_and_null_statistics(self):
        self.events = self.events[:1] + self.events[-1:]
        self.events[1]['seq'] = 2
        self.events[1]['event_id'] = 'run:2'
        self.results = {}
        report = self.write()
        result = build_corpus([report, report], self.base / 'summary')
        self.assertEqual(result['summary']['eligible_rounds'], 0)
        self.assertIsNone(result['summary']['completion']['rate'])
        self.assertIsNone(result['summary']['settlement']['score']['mean'])
        self.assertEqual(result['duplicate_sessions_excluded'], ['s1'])

    def test_skip_preparation(self):
        self.session['start_observed'] = False
        report = self.write()
        self.assertEqual(report['integrity']['entry_mode'], 'skip_preparation')
        self.assertFalse(report['complete'])

    def test_missing_reward_verification_is_unevaluated(self):
        page = self.add('reward_page_start', frame=True, selected_candidate_id='a', layout={'candidates': [{'candidate_id': 'a'}]})
        preview = self.add('reward_candidate_preview', frame=True, reward_page_id=page['event_id'], candidate={'candidate_id': 'a'})
        self.results[preview['event_id']] = {'detail_verified': True, 'observed_name': '未核验牌名'}
        report = self.write()
        self.assertIsNone(report['recognition']['reward_identity']['rate'])
        self.assertEqual(report['recognition']['reward_identity']['total'], 0)
        self.assertEqual(report['recognition']['reward_candidate_analysis']['total'], 1)

    def test_cross_session_segment_reuse(self):
        report = self.write()
        other = deepcopy(report)
        other['session']['session_id'] = 's2'
        result = build_corpus([report, other], self.base / 'summary')
        self.assertEqual(result['summary']['sessions'], 0)
        self.assertEqual(result['shared_segments_excluded'], {'run': ['s1', 's2']})

    def test_failed_current_analysis_is_reported(self):
        self.write()
        status = self.capture / 'run/analysis/status.json'
        status.write_text(json.dumps({'status': 'failed', 'analysis_id': 'current'}), encoding='utf8')
        report = build_report(self.directory)
        self.assertEqual(report['segments'][0]['analysis']['status'], 'failed')

    def test_old_schema_is_rejected(self):
        self.write()
        self.session['schema_version'] = 2
        (self.directory / 'data/session.json').write_text(json.dumps(self.session), encoding='utf-8')
        with self.assertRaises(ValueError):
            build_report(self.directory)

    def test_event_tables_screenshots_and_browser_report(self):
        self.events.pop()
        event = self.add("decision_before", operation="play_card", selected_box=[1, 2, 3, 4], frame=True)
        self.results[event['event_id']] = {'source_frame_sha256': event['frame']['sha256'], 'cards': [
            {'box': [1, 2, 3, 4], 'identity': {'database_name': '卡片甲'}},
            {'box': [5, 6, 3, 4], 'identity': {'database_name': '卡片乙'}}]}
        self.add('produce_session_end', terminal=True, reason='returned_home')
        self.write()
        html = (self.directory / 'report.html').read_text(encoding='utf-8')
        self.assertIn('1. 卡片甲（已选择）', html)
        self.assertIn('2. 卡片乙', html)
        self.assertIn('src="data/run/frames/', html)
        self.assertGreater(html.count('<table>'), 1)
        self.assertEqual({p.name for p in self.directory.iterdir()}, {'data', 'report.html'})

    def test_battle_operations_grouped_by_invocation_not_adjacency(self):
        self.events.pop()
        self.add("battle_invocation_start", battle_id=None)
        self.add("decision_before", battle_id="battle-one", operation="play_card")
        self.add("decision_after", battle_id="battle-one", decision_id="run:7")
        self.add("decision_before", battle_id="battle-one", operation="use_drink_task")
        self.add("battle_invocation_end", battle_id="battle-one")
        self.add("battle_invocation_start", battle_id=None)
        self.add("decision_before", battle_id="battle-two", operation="skip_round")
        self.add("battle_invocation_end", battle_id="battle-two")
        self.add("produce_session_end", terminal=True, reason="returned_home")
        self.write()
        text = (self.directory / "report.html").read_text(encoding="utf-8")
        self.assertEqual(text.count("<h3>战斗 "), 2)
        self.assertEqual(text.count('<table class="battle">'), 2)
        first, second = text.split("<h3>战斗 2", 1)
        self.assertIn("使用饮料", first)
        self.assertIn("出牌", first)
        self.assertNotIn("跳过回合", first)
        self.assertIn("跳过回合", second)

    def test_machine_export_is_explicit(self):
        self.write()
        self.assertFalse((self.directory / "data/report.json").exists())
        build_report(self.directory, export_data=True)
        self.assertTrue((self.directory / "data/report.json").exists())

    def test_retry_report_distinguishes_lowering_attempt_and_confirmation(self):
        self.add("pipeline_action", node="ProduceNIAFailedRetryLastChallenge", framework_action_succeeded=True)
        self.add("pipeline_action", node="ProduceNIAFailedRetryConfirm", framework_action_succeeded=True)
        self.add("pipeline_action", node="ProduceFailedRetryConfirm", framework_action_succeeded=False)
        self.write()
        text = (self.directory / "report.html").read_text(encoding="utf-8")
        self.assertIn("尝试选择上次挑战的下一档试镜（实际档位未核验）", text)
        self.assertIn("确认重试试镜", text)
        self.assertIn("确认重试考试（执行失败）", text)

    def test_result_stream_reuses_observation_and_ignores_partial_tail(self):
        from produce.audit import analysis_records
        path = self.base / "results.jsonl"
        path.write_text(json.dumps({"event_id": "one", "observation": {"cards": ["synthetic"]}}) + "\n"
                        + json.dumps({"event_id": "two", "result_ref": "one"}) + "\n" + '{"event_id":', encoding="utf-8")
        rows = list(analysis_records(path))
        self.assertEqual(len(rows), 2)
        self.assertIs(rows[0][1], rows[1][1])

    def test_user_report_has_no_raw_debug_payload(self):
        self.write()
        text = (self.directory / 'report.html').read_text(encoding='utf-8')
        self.assertIn('选择 休息', text)
        self.assertIn('分数：</span><strong>12000', text)
        self.assertNotIn('```', text)
        self.assertNotIn('sha256', text)
        self.assertNotIn('framework_action_succeeded', text)
        self.assertNotIn('run:3', text)
        self.assertNotIn("<script", text)

    def test_resume_duration_and_fresh_start_contamination(self):
        self.events[-1].update(terminal=False, reason='task_ended_without_home')
        self.write()
        second = self.capture / 'run2'
        second.mkdir()
        events = [
            {'kind': 'produce_session_resume', 'continuity': 'explicit_resume_with_matching_configuration'},
            {'kind': 'produce_gameplay_entry', 'fresh_start': False},
            {'kind': 'produce_session_end', 'terminal': True, 'reason': 'returned_home'},
        ]
        for n, e in enumerate(events, 1):
            e.update(seq=n, event_id=f'run2:{n}', run_id='run2', segment_id='run2', session_id='s1', time=f'2000-01-01T01:00:{n:02d}+00:00')
        manifest = {'run_id': 'run2', 'status': 'closed', 'events_written': 3, 'events_dropped': 0,
                    'started_at': '2000-01-01T01:00:00+00:00', 'ended_at': '2000-01-01T01:01:00+00:00',
                    'metadata': {'session_id': 's1', 'bindings': self.bindings, 'entry': 'ProduceSkipPreparation', 'version_files': {'agent.py': 'hash'}}}
        (second / 'manifest.json').write_text(json.dumps(manifest), encoding='utf8')
        (second / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events), encoding='utf8')
        self.session['segments'] = [{'run_id': 'run', 'relative_path': 'data/run'}, {'run_id': 'run2', 'relative_path': 'data/run2'}]
        self.session['ended_at'] = manifest['ended_at']
        (self.directory / 'data/session.json').write_text(json.dumps(self.session), encoding='utf8')
        report = build_report(self.directory)
        self.assertEqual(report['integrity']['entry_mode'], 'interrupted_resume')
        self.assertEqual(report['timing']['active_seconds'], 120)
        self.assertEqual(report['timing']['wall_seconds'], 3660)
        self.assertFalse(report['complete'])
        events[1]['fresh_start'] = True
        (second / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events), encoding='utf8')
        report = build_report(self.directory)
        self.assertIn('fresh_start_in_resume', self.codes(report))

    def test_temporary_evidence_has_no_dead_image_links(self):
        for event in self.events:
            if event.get("frame"):
                event["frame"]["retention"] = "temporary"
                (self.capture / "run" / event["frame"]["path"]).unlink()
        report = self.write()
        self.assertNotIn("frame_integrity", self.codes(report))
        self.assertTrue(all(row["evidence"]["frame_path"] is None for row in report["timeline"]))
        self.assertNotIn("<img ", (self.directory / "report.html").read_text(encoding="utf-8"))

    def test_display_directory_and_automatic_final_report(self):
        import os

        from produce.reporting import process_alive, schedule_report
        self.write()
        original = self.capture / "run"
        new = self.directory / "segments/run"
        new.parent.mkdir()
        original.rename(new)
        self.session["segments"] = [{"run_id": "run", "relative_path": "segments/run"}]
        (self.directory / "data/session.json").write_text(json.dumps(self.session), encoding="utf-8")
        (self.directory / "data/report_pending.json").write_text("{}", encoding="utf-8")
        schedule_report(self.directory)
        self.assertFalse((self.directory / "data/report_pending.json").exists())
        status = json.loads((self.directory / "data/report_status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "completed")
        self.assertTrue((self.directory / "report.html").is_file())
        self.assertTrue(process_alive(os.getpid()))

    def test_report_includes_battle_and_option_operations(self):
        self.events.pop()
        self.add("decision_before", operation="play_card", selected_box=[1, 2, 3, 4], reason="game_recommendation")
        self.add("option_choice", title="工作选择", action="点击第2项", actual_point=[100, 880])
        self.add("produce_session_end", terminal=True, reason="returned_home")
        report = self.write()
        kinds = {row["kind"] for row in report["timeline"]}
        self.assertIn("decision_before", kinds)
        self.assertIn("option_choice", kinds)
        html = (self.directory / "report.html").read_text(encoding="utf-8")
        self.assertIn("· 工作选择", html)
        self.assertIn('<th scope="row">实际操作</th><td>点击第2项</td>', html)
        self.assertIn("培育回放", html)

    def test_report_shows_recorded_card_adjustments(self):
        for node in ("ProduceChooseStrengthenFlag", "ProduceChooseStrengthen", "ProduceChooseDeleteFlag", "ProduceChooseDeleteClick"):
            before = self.add("route_before", node=node, frame=True)
            self.add("route_input_result", node=node, decision_id=before["event_id"], framework_action_succeeded=True)
        self.write()
        html = (self.directory / "report.html").read_text(encoding="utf-8")
        self.assertIn("确认强化", html)
        self.assertIn("选择删除、复制或变换对象", html)
        self.assertIn("实际效果未确认", html)

    def test_report_shows_icon_names_without_claiming_availability(self):
        for node in ("ProduceChooseStrengthenFlag", "ProduceChooseDeleteClick", "ProduceShoppingBuy"):
            event = self.add("route_before", node=node, frame=True)
            self.results[event["event_id"]] = {
                "icon_matches": [{"database_name": "合成<卡>", "variant_verified": False}] * 2}
        event = self.add("route_before", node="ProduceShoppingExit", frame=True)
        self.results[event["event_id"]] = {"icon_matches": []}
        event = self.add("move_cards_screen", operation="move_cards", frame=True)
        self.results[event["event_id"]] = {"icon_matches": [{"database_name": "移动卡"}]}
        report = self.write()
        html = Path(report["report_path"]).read_text(encoding="utf-8")
        self.assertEqual(html.count("合成&lt;卡&gt; ×2"), 3)
        self.assertIn("移动卡 ×1", html)
        self.assertIn("未命中缓存中的已知图标", html)
        self.assertIn("强化、可选状态及实际获得情况未核验", html)
        self.assertNotIn("<卡>", html)
        rows = [row for row in report["timeline"] if row["kind"] == "route_before"]
        self.assertEqual(len(rows[0]["detail"]["icon_matches"]), 2)

    def test_report_collapses_images_and_omits_empty_choice_changes(self):
        self.add("option_choice", title="工作选择", action="点击第2项", frame=True)
        self.add("option_choice", title="属性选择", action="点击声乐")
        self.add("reward_receive_input", framework_action_succeeded=True)
        self.write()
        html = (self.directory / "report.html").read_text(encoding="utf-8")
        self.assertIn('<details>\n<summary>查看截图</summary>\n<div class="pictures">', html)
        for section in html.split("<article><h3>")[1:]:
            if any(name in section.splitlines()[0] for name in ("工作选择", "属性选择", "奖励选择")):
                self.assertNotIn('<th scope="row">观察变化</th><td>未确认</td>', section)
        for section in html.split("<details>")[1:]:
            self.assertIn("<img ", section.split("</details>")[0])
        self.assertEqual(html.count("<details>"), html.count("</details>"))

    def test_report_finishes_without_worker(self):
        from produce.reporting import schedule_report
        self.write()
        schedule_report(self.directory)
        self.assertFalse((self.directory / "data/report_worker.json").exists())
        status = json.loads((self.directory / "data/report_status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "completed", status)
        self.assertFalse((self.directory / "data/report_pending.json").exists())

    def test_html_escapes_content_and_encodes_external_output_image_paths(self):
        from html.parser import HTMLParser
        from urllib.parse import unquote

        self.bindings['configured_idol']['song_name'] = '<script>alert("song")</script> & 歌曲'
        self.add('option_choice', title='<img src=x onerror=alert(1)>', action='甲 | 乙\n下一行', frame=True)
        self.write()
        output = self.base / '导出 # 报告'
        report = build_report(self.directory, output)
        html = Path(report['report_path']).read_text(encoding='utf-8')
        self.assertTrue(html.startswith('<!doctype html>'))
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertIn('甲 | 乙<br>下一行', html)
        self.assertFalse((output / 'report.md').exists())

        class Images(HTMLParser):
            def __init__(self):
                super().__init__()
                self.sources = []
                self.details = []

            def handle_starttag(self, tag, attrs):
                if tag == 'img':
                    self.sources.append(dict(attrs)['src'])
                if tag == 'details':
                    self.details.append(dict(attrs))

        parsed = Images()
        parsed.feed(html)
        self.assertTrue(parsed.sources)
        self.assertTrue(parsed.details)
        self.assertTrue(all('open' not in attrs for attrs in parsed.details))
        self.assertTrue(all((output / unquote(src)).is_file() for src in parsed.sources))


class LayoutReadingTest(unittest.TestCase):
    @staticmethod
    def token(text, x, y, w=80, h=30):
        return {"text": text, "box": [x, y, w, h], "confidence": 0.99}

    def test_final_exam_requires_following_label_and_same_battle(self):
        from produce.settlement import FinalExamTracker, settlement_observation
        tokens = [self.token("ログ", 605, 460), self.token("2nd", 35, 438),
                  self.token("9,876Pt", 210, 480), self.token("1st", 35, 295),
                  self.token("20,000Pt", 210, 335)]
        for scenario in ("初", "NIA"):
            for reset in (None, "battle_invocation_start", "exam_failure_or_retry"):
                tracker = FinalExamTracker(scenario)
                ranking = settlement_observation(tokens)
                tracker.observe({"event_id": "ranking"}, ranking)
                self.assertIsNone(ranking["final_exam"]["score"]["value"])
                if reset:
                    tracker.observe({"kind": reset})
                final = settlement_observation([self.token("最終試験", 55, 110)])
                tracker.observe({"event_id": "confirmation"}, final)
                expected = 9876 if scenario == "初" and reset is None else None
                self.assertEqual(final["final_exam"]["score"]["value"], expected)
        incomplete = settlement_observation(tokens[:1] + tokens[3:])
        self.assertIsNone(incomplete["exam_ranking"])

    def test_nia_final_failure_uses_own_ranking_without_formula_score(self):
        from produce.console import SettlementConsole
        from produce.settlement import FinalExamTracker, settlement_observation
        tracker = FinalExamTracker("NIA")
        ranking = settlement_observation([self.token("ログ", 605, 460), self.token("2nd", 35, 438),
                                          self.token("12,345Pt", 210, 480)])
        tracker.observe({"event_id": "ranking"}, ranking)
        tokens = [self.token("最終オーディション", 247, 305), self.token("不合格", 336, 645)]
        final = settlement_observation(tokens)
        tracker.observe({"event_id": "final"}, final)
        result = final["final_audition"]
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["score"]["value"], 12345)
        self.assertIsNone(final["settlement"]["score"]["value"])
        console = SettlementConsole({"scenario": "NIA", "difficulty": "MASTER"})
        console.observe({"kind": "exam_result_observation"}, final)
        self.assertIn("最终试镜不合格", console.finish()["message"])
        tracker.observe({"kind": "battle_invocation_start"})
        final = settlement_observation(tokens)
        tracker.observe({"event_id": "new"}, final)
        self.assertIsNone(final["final_audition"]["score"]["value"])

    def test_lesson_layout_keeps_partial_values_with_verified_label(self):
        import numpy as np
        from produce.state import BattleStateReader
        values = {(15, 25, 170, 43): "残りターン数", (65, 65, 98, 50): "0+2",
                  (570, 169, 70, 40): "37", (655, 129, 65, 45): "0"}
        def read(image, roi):
            text = values.get(tuple(roi))
            return [self.token(text, roi[0], roi[1])] if text else []
        reader = BattleStateReader(ROOT / "assets/data/battle_buff_templates.json", read)
        image = np.zeros((1280, 720, 3), dtype=np.uint8)
        state = reader.read(image)
        self.assertTrue(state["hud_present"])
        self.assertEqual(state["fields"]["genki"]["value"], 0)
        self.assertEqual(state["fields"]["turns_remaining"]["value"], 2)
        self.assertIsNone(state["fields"]["score"]["value"])
        values[(655, 129, 65, 45)] = "O"
        partial = reader.read(image)
        self.assertTrue(partial["hud_present"])
        self.assertEqual(partial["fields"]["stamina"]["value"], 37)
        self.assertIsNone(partial["fields"]["genki"]["value"])
        from produce.console import state_lines, compact_state_lines
        for render in (state_lines, compact_state_lines):
            text = " ".join(render(partial))
            self.assertNotIn("倍率", text)
            self.assertNotIn("属性", text)
            self.assertIn("37", text)
        values.pop((570, 169, 70, 40))
        self.assertFalse(reader.read(image)["hud_present"])
        values.pop((15, 25, 170, 43))
        self.assertFalse(reader.read(image)["hud_present"])


if __name__ == '__main__':
    unittest.main(verbosity=2)
