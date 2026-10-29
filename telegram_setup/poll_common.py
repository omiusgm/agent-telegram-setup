"""Strict, portable validation for bounded personal-account polls."""
import hashlib
import json
import re


def valid_request_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{8,128}', value):
        raise ValueError('request_id_required')
    return value


def numeric_chat(value):
    if type(value) is not int or value == 0:
        raise ValueError('numeric_chat_id_required')
    return value


def positive_message(value):
    if type(value) is not int or value <= 0:
        raise ValueError('positive_message_id_required')
    return value


def _units(value):
    try:
        return len(value.encode('utf-16-le')) // 2
    except UnicodeError:
        raise ValueError('invalid_unicode') from None


def validate_poll(question, options, anonymous, multiple_choice, quiz_correct_index):
    if not isinstance(question, str) or not question.strip() or _units(question) > 255:
        raise ValueError('question_must_be_1_to_255_utf16_units')
    if not isinstance(options, list) or not 2 <= len(options) <= 12:
        raise ValueError('poll_requires_2_to_12_options')
    if any(not isinstance(x, str) or not x.strip() or _units(x) > 100 for x in options):
        raise ValueError('option_must_be_1_to_100_utf16_units')
    if len({x.strip() for x in options}) != len(options):
        raise ValueError('poll_options_must_be_distinct')
    if type(anonymous) is not bool or type(multiple_choice) is not bool:
        raise ValueError('poll_flags_must_be_boolean')
    if quiz_correct_index is not None:
        if type(quiz_correct_index) is not int or not 0 <= quiz_correct_index < len(options):
            raise ValueError('quiz_correct_index_out_of_range')
        if multiple_choice:
            raise ValueError('quiz_cannot_allow_multiple_answers')
    return {'question': question, 'options': list(options), 'anonymous': anonymous,
            'multiple_choice': multiple_choice, 'quiz_correct_index': quiz_correct_index}


def fingerprint(action, **fields):
    payload = json.dumps({'action': action, **fields}, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()
