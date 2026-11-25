from quest_ai_runner.self.integrity.prompts.auxiliary_prompts import INTENT_DIRECTIVE_PROMPT


def test_command_verb_with_inner_question_is_an_order():
    assert "OPENS with a command verb" in INTENT_DIRECTIVE_PROMPT
    assert "the question is the content to set up" in INTENT_DIRECTIVE_PROMPT


def test_create_experiment_colon_question_is_named_as_an_order():
    assert "Create an experiment: does a short walk after lunch lift my afternoon energy?" in INTENT_DIRECTIVE_PROMPT
    assert "is not a case of being torn" in INTENT_DIRECTIVE_PROMPT
