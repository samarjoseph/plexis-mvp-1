import uuid
import pytest
from memory.conversation import ConversationMemory
from core.pipeline import request_pipeline
from conversation.context_builder import build_context_block


def test_conversation_memory_client_messages_tier1():
    mem = ConversationMemory()
    session_id = "test-session-1"
    client_msgs = [
        {"role": "user", "content": "modi ji age"},
        {"role": "assistant", "content": "Narendra Modi is 76 years old."},
    ]

    history = mem.get_history(session_id=session_id, client_messages=client_msgs)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "modi ji age"
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "Narendra Modi is 76 years old."


def test_context_builder_includes_conversation_history():
    history = [
        {"role": "user", "content": "modi ji age"},
        {"role": "assistant", "content": "Narendra Modi is 76 years old."},
        {"role": "user", "content": "thnks broo"},
        {"role": "assistant", "content": "Haha, thanks!"},
    ]
    prompt = build_context_block(
        message="what i asked to you before can you tell me",
        intent="conversation",
        conversation_history=history,
    )

    assert "CONVERSATION HISTORY (most recent last):" in prompt
    assert "USER: modi ji age" in prompt
    assert "ASSISTANT: Narendra Modi is 76 years old." in prompt
    assert "USER: thnks broo" in prompt
    assert "USER: what i asked to you before can you tell me" in prompt


def test_pipeline_populates_conversation_history_with_client_messages():
    session_id = f"sess-{uuid.uuid4()}"
    client_msgs = [
        {"role": "user", "content": "modi ji age"},
        {"role": "assistant", "content": "Narendra Modi was born on 17 September 1950."},
    ]

    ctx = request_pipeline.process(
        message="what i asked to you before can you tell me",
        session_id=session_id,
        client_messages=client_msgs,
    )

    assert len(ctx.conversation_history) == 2
    assert ctx.conversation_history[0]["content"] == "modi ji age"
    assert ctx.conversation_history[1]["content"] == "Narendra Modi was born on 17 September 1950."


def test_multi_turn_unique_values_followup_resolves_column_and_executes():
    import pandas as pd
    from core.context import ExecutionContext
    from datasets.registry import dataset_registry, DatasetEntry
    from router.master_router import master_router
    from router.engine_registry import engine_registry
    from engines.analysis import analysis_engine
    from engines.conversation import conversation_engine

    engine_registry.register(analysis_engine, ['analysis'])
    engine_registry.register(conversation_engine, ['conversation'])

    df = pd.DataFrame({
        'Airline': ['Delta', 'United', 'American', 'Southwest', 'JetBlue', 'Spirit', 'Frontier', 'Alaska', 'Allegiant'],
        'Departure_Delay': [10, 20, 30, 40, 50, 60, 70, 80, 90]
    })
    entry = DatasetEntry(
        dataset_id='test_history_flight_ds',
        filename='flights.csv',
        file_path='',
        upload_timestamp='',
        row_count=len(df),
        column_count=len(df.columns),
        file_fingerprint='fp_hist',
        schema_fingerprint='sfp_hist',
        dataframe=df,
        profile={},
        column_profiles={},
        schema_profile=[{'name': 'Airline', 'type': 'string'}, {'name': 'Departure_Delay', 'type': 'numeric'}]
    )
    dataset_registry.register(entry)

    # 1. Turn 1 query
    ctx1 = ExecutionContext(
        session_id='sess_turn1',
        message='how many airlines are there in the dataset',
        dataset_id='test_history_flight_ds',
        dataset_filename='flights.csv'
    )
    ctx1.schema_profile = entry.schema_profile
    res1 = master_router.route(ctx1)
    assert ctx1.intent == 'analysis'
    assert getattr(ctx1, 'llm_operation', {}).get('type') == 'UNIQUE_COUNT'
    assert getattr(ctx1, 'llm_operation', {}).get('column') == 'Airline'

    # 2. Turn 2 follow-ups referencing turn 1 context
    followup_queries = [
        'give all 9 names',
        'give me all 9 names',
        'what are their names',
        'list all 9 names',
        'show them',
        'name them',
        'who are they',
    ]
    for q in followup_queries:
        ctx2 = ExecutionContext(
            session_id=f'sess_followup_{hash(q)}',
            message=q,
            dataset_id='test_history_flight_ds',
            dataset_filename='flights.csv'
        )
        ctx2.schema_profile = entry.schema_profile
        ctx2.conversation_history = [
            {'role': 'user', 'content': 'how many airlines are there in the dataset'},
            {'role': 'assistant', 'content': 'There are 9 distinct airlines represented in the dataset. 😊 Is there anything specific you\'d like to explore about those airlines?'}
        ]
        res2 = master_router.route(ctx2)
        assert ctx2.intent == 'analysis', f'Failed intent for {q}'
        assert getattr(ctx2, 'llm_operation', {}).get('type') == 'UNIQUE_VALUES', f'Failed op type for {q}'
        assert getattr(ctx2, 'llm_operation', {}).get('column') == 'Airline', f'Failed col for {q}'
        assert 'Delta' in res2.answer and 'United' in res2.answer


def test_direct_unique_values_queries():
    import pandas as pd
    from core.context import ExecutionContext
    from datasets.registry import dataset_registry, DatasetEntry
    from router.master_router import master_router

    df = pd.DataFrame({
        'Airline': ['Delta', 'United', 'American', 'Southwest', 'JetBlue', 'Spirit', 'Frontier', 'Alaska', 'Allegiant'],
        'Departure_Delay': [10, 20, 30, 40, 50, 60, 70, 80, 90]
    })
    entry = DatasetEntry(
        dataset_id='test_direct_flight_ds',
        filename='flights.csv',
        file_path='',
        upload_timestamp='',
        row_count=len(df),
        column_count=len(df.columns),
        file_fingerprint='fp_dir',
        schema_fingerprint='sfp_dir',
        dataframe=df,
        profile={},
        column_profiles={},
        schema_profile=[{'name': 'Airline', 'type': 'string'}, {'name': 'Departure_Delay', 'type': 'numeric'}]
    )
    dataset_registry.register(entry)

    direct_queries = [
        'give all 9 airlines',
        'give all airlines',
        'list the airlines',
        'what are the airlines',
        'what are the 9 airlines',
        'show all 9 airlines',
        'list all distinct airlines',
    ]
    for q in direct_queries:
        ctx = ExecutionContext(
            session_id=f'sess_dir_{hash(q)}',
            message=q,
            dataset_id='test_direct_flight_ds',
            dataset_filename='flights.csv'
        )
        ctx.schema_profile = entry.schema_profile
        res = master_router.route(ctx)
        assert ctx.intent == 'analysis', f'Failed intent for {q}'
        assert getattr(ctx, 'llm_operation', {}).get('type') == 'UNIQUE_VALUES', f'Failed op for {q}'
        assert getattr(ctx, 'llm_operation', {}).get('column') == 'Airline', f'Failed col for {q}'


def test_non_mutating_requests_never_routed_to_transform():
    from router.master_router import _has_mutation_intent

    non_mutating = [
        'give all 9 names',
        'give me the names',
        'how many airlines are there in the dataset',
        'what is the average delay',
        'show me the top 10 airlines',
        'list the airlines',
        'who has the lowest delay',
    ]
    for q in non_mutating:
        assert not _has_mutation_intent(q), f'{q} should not have mutation intent'

    mutating = [
        'drop the flight_number column',
        'drop column flight_number',
        'fill missing values with 0',
        'replace old with new',
        'rename airline to airline_name',
        'add a column score',
        'clip delay at 60',
    ]
    for q in mutating:
        assert _has_mutation_intent(q), f'{q} should have mutation intent'

