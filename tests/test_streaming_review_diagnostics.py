"""Offline transport diagnostics; all requests are mocked and contain invented data."""
import json
import os
import unittest
from unittest.mock import Mock, patch

import requests
from features.streaming_review import (ANSWER_SCHEMA, MAX_RESPONSE_BYTES, READ_TIMEOUT_SECONDS, REASONS,
                                       ModelRequestError, _request, review_plans)


class RequestDiagnosticsTests(unittest.TestCase):
    def response(self, raw=None, status=200):
        response = Mock(status_code=status)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [raw if raw is not None else json.dumps(
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"ok":true}'}]}).encode()]
        return response

    def failure(self, response, code, **extra):
        with patch.dict(os.environ, {}, clear=True), patch(
                'features.streaming_review.requests.post', return_value=response) as post:
            with self.assertRaises(ModelRequestError) as caught:
                _request('private fixture payload', 'fixture-model', 'fixture-secret')
            post.assert_called_once()
            self.assertFalse(post.call_args.kwargs['allow_redirects'])
            self.assertEqual(caught.exception.diagnostic, {'code':code, 'http_status':response.status_code, **extra})
            self.assertEqual(str(caught.exception), code)
            self.assertNotIn('fixture-secret', json.dumps(caught.exception.diagnostic))
            self.assertNotIn('private', json.dumps(caught.exception.diagnostic))
        return caught.exception

    def test_http_rejections_retain_status_without_reading_private_body(self):
        for status in (400, 401, 404, 429, 500, 302):
            with self.subTest(status=status):
                response = self.response(b'private error details fixture-secret', status)
                self.failure(response, 'http_error')
                response.iter_content.assert_not_called()

    def test_invalid_transport_json_is_distinguished_from_answer_json(self):
        self.failure(self.response(b'private invalid envelope'), 'invalid_envelope_json')
        body = {'stop_reason':'end_turn','content':[{'type':'text','text':'private invalid answer'}]}
        self.failure(self.response(json.dumps(body).encode()), 'invalid_answer_json')

    def test_invalid_utf8_does_not_escape_or_echo_content(self):
        self.failure(self.response(b'\xffprivate'), 'invalid_envelope_json')

    def test_invalid_envelope_shape_has_separate_code(self):
        self.failure(self.response(b'[]'), 'invalid_envelope_shape')

    def test_truncation_retains_safe_stop_reason(self):
        self.failure(self.response(b'{"stop_reason":"max_tokens"}'), 'incomplete_response', stop_reason='max_tokens')

    def test_unknown_stop_reason_is_redacted(self):
        self.failure(self.response(b'{"stop_reason":"private fixture-secret"}'), 'incomplete_response', stop_reason='unknown')

    def test_thinking_blocks_are_ignored_and_never_returned(self):
        # A model that always thinks returns reasoning blocks before the answer.
        for reasoning in ({'type':'thinking','thinking':'','signature':'fixture'},
                          {'type':'thinking','thinking':'private fixture-secret','signature':'fixture'},
                          {'type':'redacted_thinking','data':'private fixture-secret'}):
            with self.subTest(block=reasoning['type']):
                body = {'stop_reason':'end_turn','content':[reasoning, {'type':'text','text':'{"ok":true}'}]}
                with patch('features.streaming_review.requests.post', return_value=self.response(json.dumps(body).encode())):
                    self.assertEqual(_request('{}','fixture','fixture-secret'), {'ok':True})

    def test_answers_other_than_one_text_block_still_fail_closed(self):
        think = {'type':'thinking','thinking':'private'}
        text = {'type':'text','text':'{}'}
        tool = {'type':'tool_use','id':'private','name':'private','input':{}}
        for content, types in (([], []), ([think], ['thinking']), ([text, text], ['text']),
                               ([think, text, text], ['text', 'thinking']), ([think, tool], ['thinking', 'tool_use']),
                               ([text, tool], ['text', 'tool_use'])):
            with self.subTest(types=types, count=len(content)):
                body = {'stop_reason':'end_turn','content':content}
                self.failure(self.response(json.dumps(body).encode()), 'unsupported_content_shape', content_types=types)

    def test_non_string_or_missing_text_is_diagnosed(self):
        for block, types in (({'type':'text'}, ['text']), ({'type':'text','text':3}, ['text']), ('private', ['unknown'])):
            self.failure(self.response(json.dumps({'stop_reason':'end_turn','content':[block]}).encode()),
                         'unsupported_content_shape', content_types=types)

    def test_unknown_block_type_names_are_redacted(self):
        body = {'stop_reason':'end_turn','content':[{'type':'private fixture-secret'}, {'type':'text','text':'{}'}]}
        self.failure(self.response(json.dumps(body).encode()), 'unsupported_content_shape', content_types=['text', 'unknown'])

    def test_malformed_block_types_are_diagnosed_once(self):
        body = {'stop_reason':'end_turn','content':[{'type':['private']}, {'type':{'private':1}}, {'type':'private'}, {}]}
        self.failure(self.response(json.dumps(body).encode()), 'unsupported_content_shape', content_types=['unknown'])

    def test_non_list_content_is_diagnosed(self):
        body = {'stop_reason':'end_turn','content':'private'}
        self.failure(self.response(json.dumps(body).encode()), 'unsupported_content_shape', content_types=[])

    def test_oversized_response_is_distinguished(self):
        self.failure(self.response(b'x'*(MAX_RESPONSE_BYTES + 1)), 'response_too_large')

    def test_request_asks_for_the_answer_schema(self):
        with patch('features.streaming_review.requests.post', return_value=self.response()) as post:
            _request('{}','fixture','fixture-secret')
        self.assertEqual(post.call_args.kwargs['json']['output_config'],
                         {'format': {'type':'json_schema','schema':ANSWER_SCHEMA}})

    def test_answer_schema_stays_within_what_the_api_accepts(self):
        def walk(node):
            self.assertFalse(set(node) & {'minItems','maxItems','minimum','maximum','minLength','maxLength'})
            if node['type'] == 'object':
                self.assertIs(node['additionalProperties'], False)
                self.assertEqual(set(node['required']), set(node['properties']))
                for child in node['properties'].values():
                    walk(child)
            if node['type'] == 'array':
                walk(node['items'])
        walk(ANSWER_SCHEMA)
        json.dumps(ANSWER_SCHEMA)

    def test_answer_schema_matches_the_validator(self):
        item = ANSWER_SCHEMA['properties']['comparisons']['items']
        self.assertEqual(set(ANSWER_SCHEMA['properties']), {'selected_plan_id','comparisons'})
        self.assertEqual(set(item['properties']), {'plan_id','reason','player_ids','evidence_ids'})
        self.assertEqual(set(item['properties']['reason']['enum']), REASONS)

    def test_read_timeout_covers_a_full_answer_arriving_at_once(self):
        with patch('features.streaming_review.requests.post', return_value=self.response()) as post:
            _request('{}','fixture','fixture-secret')
        self.assertEqual(post.call_args.kwargs['timeout'][1], READ_TIMEOUT_SECONDS)
        self.assertGreaterEqual(READ_TIMEOUT_SECONDS, 120)

    def test_transport_error_without_response_does_not_imply_no_billing(self):
        with patch('features.streaming_review.requests.post', side_effect=requests.Timeout('private fixture-secret')) as post:
            with self.assertRaises(ModelRequestError) as caught:
                _request('{}','fixture','fixture-secret')
            self.assertEqual(caught.exception.diagnostic, {'code':'transport_error'})
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__)
            post.assert_called_once()

    def test_stream_failure_preserves_received_http_status(self):
        response = self.response()
        response.iter_content.side_effect = requests.ConnectionError('private fixture-secret')
        self.failure(response, 'transport_error')

    def test_optional_diagnostics_capture_only_safe_usage_fields(self):
        body={'stop_reason':'end_turn','usage':{'input_tokens':100,'output_tokens':20,
              'cache_read_input_tokens':False,'private':'fixture-secret'},
              'content':[{'type':'text','text':'{"ok":true}'}]}
        diagnostics={}
        with patch('features.streaming_review.requests.post',return_value=self.response(json.dumps(body).encode())):
            self.assertEqual(_request('{}','fixture','fixture-secret',diagnostics=diagnostics), {'ok':True})
        self.assertEqual(diagnostics, {'http_status':200,'usage':{'input_tokens':100,'output_tokens':20}})

    def test_success_keeps_existing_return_contract(self):
        with patch('features.streaming_review.requests.post', return_value=self.response()) as post:
            self.assertEqual(_request('{}','fixture','fixture-secret'), {'ok':True})
            post.assert_called_once()

    def test_review_fallback_keeps_safe_request_diagnostic(self):
        # All domain helpers mocked: this test checks only error propagation.
        result={'deterministic_plan_id':'hold','snapshot':{'players':[], 'as_of':'fixture','rules':{}},'limitations':[]}
        plans=[{'plan_id':'hold','actions':[],'calendar':[]},{'plan_id':'fixture','actions':[],'calendar':[]}]
        context={'entries':[],'coverage':[],'limitations':[]}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY':'fixture-secret'}, clear=True), \
                patch('features.streaming_review._shortlist', return_value=plans), \
                patch('features.streaming_review.validate_plan'), \
                patch('features.streaming_review._request', side_effect=ModelRequestError('http_error',http_status=404)):
            review=review_plans(result,context,enabled=True,model='fixture')
        self.assertEqual(review['status'],'error_or_invalid_output')
        self.assertEqual(review['selected_plan_id'],'hold')
        self.assertEqual(review['request_diagnostic'], {'code':'http_error','http_status':404})
        self.assertNotIn('fixture-secret',json.dumps(review))

    def test_answer_rejected_after_200_keeps_status_and_usage(self):
        # A received, possibly billed response must stay distinguishable from a pre-dispatch failure.
        result={'deterministic_plan_id':'hold','snapshot':{'players':[], 'as_of':'fixture','rules':{}},'limitations':[]}
        plans=[{'plan_id':'hold','actions':[],'calendar':[]},{'plan_id':'fixture','actions':[],'calendar':[]}]
        context={'entries':[],'coverage':[],'limitations':[]}
        raw=json.dumps({"stop_reason":"end_turn","usage":{"input_tokens":100,"output_tokens":20},
                        "content":[{"type":"text","text":'{"unexpected":true}'}]}).encode()
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY':'fixture-secret'}, clear=True), \
                patch('features.streaming_review._shortlist', return_value=plans), \
                patch('features.streaming_review.validate_plan'), \
                patch('features.streaming_review.requests.post', return_value=self.response(raw)):
            review=review_plans(result,context,enabled=True,model='fixture')
        self.assertEqual(review['status'],'error_or_invalid_output')
        self.assertEqual(review['request_diagnostic'],
                         {'code':'answer_failed_validation','http_status':200,
                          'validation_failure':'Unexpected response fields',
                          'usage':{'input_tokens':100,'output_tokens':20}})
        self.assertNotIn('fixture-secret',json.dumps(review))

    def test_failure_from_plan_checks_is_not_echoed(self):
        # validate_plan messages aren't fixed text, so they stay out of exports.
        result={'deterministic_plan_id':'hold','snapshot':{'players':[], 'as_of':'fixture','rules':{}},'limitations':[]}
        plans=[{'plan_id':'hold','actions':[],'calendar':[]},{'plan_id':'fixture','actions':[],'calendar':[]}]
        context={'entries':[],'coverage':[],'limitations':[]}
        answer={'selected_plan_id':'hold','comparisons':[{'plan_id':'hold','reason':'hold','player_ids':[],'evidence_ids':[]}]}
        raw=json.dumps({"stop_reason":"end_turn","content":[{"type":"text","text":json.dumps(answer)}]}).encode()
        calls=[None, None, ValueError('private fixture-secret')]
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY':'fixture-secret'}, clear=True), \
                patch('features.streaming_review._shortlist', return_value=plans), \
                patch('features.streaming_review.validate_plan', side_effect=calls), \
                patch('features.streaming_review.requests.post', return_value=self.response(raw)):
            review=review_plans(result,context,enabled=True,model='fixture')
        self.assertEqual(review['request_diagnostic'], {'code':'answer_failed_validation','http_status':200})
        self.assertNotIn('private',json.dumps(review))


if __name__=='__main__':
    unittest.main()
