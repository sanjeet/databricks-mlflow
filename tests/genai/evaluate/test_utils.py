import json
import os
from typing import Any, Literal
from unittest.mock import patch

import pandas as pd
import pytest

import mlflow
from mlflow.entities.assessment import Expectation, Feedback
from mlflow.entities.assessment_source import AssessmentSource
from mlflow.entities.span import SpanType
from mlflow.entities.trace import Trace
from mlflow.exceptions import MlflowException
from mlflow.genai import scorer
from mlflow.genai.evaluation.utils import (
    _convert_scorer_to_legacy_metric,
    _convert_to_legacy_eval_set,
)
from mlflow.genai.scorers.builtin_scorers import Safety
from mlflow.utils.spark_utils import is_spark_connect_mode


@pytest.fixture(scope="module")
def spark():
    # databricks-agents installs databricks-connect
    if is_spark_connect_mode():
        pytest.skip("Local Spark Session is not supported when databricks-connect is installed.")

    from pyspark.sql import SparkSession

    with SparkSession.builder.getOrCreate() as spark:
        yield spark


def count_rows(data: Any) -> int:
    try:
        from mlflow.utils.spark_utils import get_spark_dataframe_type

        if isinstance(data, get_spark_dataframe_type()):
            return data.count()
    except Exception:
        pass

    return len(data)


@pytest.fixture
def sample_dict_data_single():
    return [
        {
            "inputs": {"question": "What is Spark?"},
            "outputs": "actual response for first question",
            "expectations": {"expected_response": "expected response for first question"},
        },
    ]


@pytest.fixture
def sample_dict_data_multiple():
    return [
        {
            "inputs": {"question": "What is Spark?"},
            "outputs": "actual response for first question",
            "expectations": {"expected_response": "expected response for first question"},
        },
        {
            "inputs": {"question": "How can you minimize data shuffling in Spark?"},
            "outputs": "actual response for second question",
            "expectations": {"expected_response": "expected response for second question"},
        },
        # Some records might not have expectations
        {
            "inputs": {"question": "What is MLflow?"},
            "outputs": "actual response for third question",
            "expectations": {},
        },
    ]


@pytest.fixture
def sample_dict_data_multiple_with_custom_expectations():
    return [
        {
            "inputs": {"question": "What is Spark?"},
            "outputs": "actual response for first question",
            "expectations": {
                "expected_response": "expected response for first question",
                "my_custom_expectation": "custom expectation for the first question",
            },
        },
        {
            "inputs": {"question": "How can you minimize data shuffling in Spark?"},
            "outputs": "actual response for second question",
            "expectations": {
                "expected_response": "expected response for second question",
                "my_custom_expectation": "custom expectation for the second question",
            },
        },
        # Some records might not have all expectations
        {
            "inputs": {"question": "What is MLflow?"},
            "outputs": "actual response for third question",
            "expectations": {
                "my_custom_expectation": "custom expectation for the third question",
            },
        },
    ]


@pytest.fixture
def sample_pd_data(sample_dict_data_multiple):
    """Returns a pandas DataFrame with sample data"""
    return pd.DataFrame(sample_dict_data_multiple)


@pytest.fixture
def sample_spark_data(sample_pd_data, spark):
    """Convert pandas DataFrame to PySpark DataFrame"""
    return spark.createDataFrame(sample_pd_data)


@pytest.fixture
def sample_spark_data_with_string_columns(sample_pd_data, spark):
    # Cast inputs and expectations columns to string
    df = sample_pd_data.copy()
    df["inputs"] = df["inputs"].apply(json.dumps)
    df["expectations"] = df["expectations"].apply(json.dumps)
    return spark.createDataFrame(df)


_ALL_DATA_FIXTURES = [
    "sample_dict_data_single",
    "sample_dict_data_multiple",
    "sample_dict_data_multiple_with_custom_expectations",
    "sample_pd_data",
    "sample_spark_data",
    "sample_spark_data_with_string_columns",
]


class TestModel:
    @mlflow.trace(span_type=SpanType.AGENT)
    def predict(self, question: str) -> str:
        response = self.call_llm(messages=[{"role": "user", "content": question}])
        return response["choices"][0]["message"]["content"]

    @mlflow.trace(span_type=SpanType.LLM)
    def call_llm(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {"choices": [{"message": {"role": "assistant", "content": "I don't know"}}]}


def get_test_traces(type=Literal["pandas", "list"]):
    model = TestModel()

    model.predict("What is MLflow?")
    traces = mlflow.search_traces(return_type=type, order_by=["timestamp_ms ASC"])

    # Add assessments. Since log_assessment API is not supported in OSS MLflow yet, we
    # need to add it to the trace info manually.
    source = AssessmentSource(source_id="test", source_type="HUMAN")
    trace = traces[0] if type == "list" else Trace.from_json(traces.iloc[0]["trace"])
    trace.info.assessments.extend(
        [
            # 1. Expectation with reserved name "expected_response"
            Expectation(
                name="expected_response",
                source=source,
                trace_id=trace.info.trace_id,
                value="expected response for first question",
            ),
            # 2. Expectation with reserved name "expected_facts"
            Expectation(
                name="expected_facts",
                source=source,
                trace_id=trace.info.trace_id,
                value=["fact1", "fact2"],
            ),
            # 3. Expectation with reserved name "guidelines"
            Expectation(
                name="guidelines",
                source=source,
                trace_id=trace.info.trace_id,
                value=["Be polite", "Be kind"],
            ),
            # 4. Expectation with custom name "ground_truth"
            Expectation(
                name="my_custom_expectation",
                source=source,
                trace_id=trace.info.trace_id,
                value="custom expectation for the first question",
            ),
            # 5. Non-expectation assessment
            Feedback(
                name="feedback",
                source=source,
                trace_id=trace.info.trace_id,
                value="some feedback",
            ),
        ]
    )
    if type == "pandas":
        traces.at[0, "trace"] = trace.to_json()
    else:
        traces = [{"trace": trace} for trace in traces]
    return traces


@pytest.mark.parametrize("input_type", ["list", "pandas"])
def test_convert_to_legacy_eval_traces(input_type):
    sample_data = get_test_traces(type=input_type)
    data = _convert_to_legacy_eval_set(sample_data)

    assert "trace" in data.columns

    # "request" column should be derived from the trace
    assert "request" in data.columns
    assert list(data["request"]) == [{"question": "What is MLflow?"}]
    assert data["expectations"][0] == {
        "expected_response": "expected response for first question",
        "expected_facts": ["fact1", "fact2"],
        "guidelines": ["Be polite", "Be kind"],
        "my_custom_expectation": "custom expectation for the first question",
    }
    # Assessment with type "Feedback" should not be present in the transformed data
    assert "feedback" not in data.columns


@pytest.mark.parametrize("data_fixture", _ALL_DATA_FIXTURES)
def test_convert_to_legacy_eval_set_has_no_errors(data_fixture, request):
    sample_data = request.getfixturevalue(data_fixture)

    transformed_data = _convert_to_legacy_eval_set(sample_data)

    assert "request" in transformed_data.columns
    assert "response" in transformed_data.columns
    assert "expectations" in transformed_data.columns


def test_convert_to_legacy_eval_raise_for_invalid_json_columns(spark):
    # Data with invalid `inputs` column
    df = spark.createDataFrame(
        [
            {"inputs": "invalid json", "expectations": '{"expected_response": "expected"}'},
            {"inputs": "invalid json", "expectations": '{"expected_response": "expected"}'},
        ]
    )
    with pytest.raises(MlflowException, match="Failed to parse `inputs` column."):
        _convert_to_legacy_eval_set(df)

    # Data with invalid `expectations` column
    df = spark.createDataFrame(
        [
            {
                "inputs": '{"question": "What is the capital of France?"}',
                "expectations": "invalid expectations",
            },
            {
                "inputs": '{"question": "What is the capital of Germany?"}',
                "expectations": "invalid expectations",
            },
        ]
    )
    with pytest.raises(MlflowException, match="Failed to parse `expectations` column."):
        _convert_to_legacy_eval_set(df)


@pytest.mark.parametrize("data_fixture", _ALL_DATA_FIXTURES)
def test_scorer_receives_correct_data(data_fixture, request):
    sample_data = request.getfixturevalue(data_fixture)

    received_args = []

    @scorer
    def dummy_scorer(inputs, outputs, expectations):
        received_args.append(
            (
                inputs["question"],
                outputs,
                expectations.get("expected_response"),
                expectations.get("my_custom_expectation"),
            )
        )
        return 0

    mlflow.genai.evaluate(
        data=sample_data,
        scorers=[dummy_scorer],
    )

    all_inputs, all_outputs, all_expectations, all_custom_expectations = zip(*received_args)
    row_count = count_rows(sample_data)
    expected_inputs = [
        "What is Spark?",
        "How can you minimize data shuffling in Spark?",
        "What is MLflow?",
    ][:row_count]
    expected_outputs = [
        "actual response for first question",
        "actual response for second question",
        "actual response for third question",
    ][:row_count]
    expected_expectations = [
        "expected response for first question",
        "expected response for second question",
        None,
    ][:row_count]

    assert set(all_inputs) == set(expected_inputs)
    assert set(all_outputs) == set(expected_outputs)
    assert set(all_expectations) == set(expected_expectations)

    if data_fixture == "sample_dict_data_multiple_with_custom_expectations":
        expected_custom_expectations = [
            "custom expectation for the first question",
            "custom expectation for the second question",
            "custom expectation for the third question",
        ]
        assert set(all_custom_expectations) == set(expected_custom_expectations)


def test_input_is_required_if_trace_is_not_provided():
    with patch("mlflow.models.evaluate") as mock_evaluate:
        with pytest.raises(MlflowException, match="inputs.*required"):
            mlflow.genai.evaluate(
                data=pd.DataFrame({"outputs": ["Paris"]}),
                scorers=[Safety()],
            )

        mock_evaluate.assert_not_called()

        mlflow.genai.evaluate(
            data=pd.DataFrame(
                {"inputs": [{"question": "What is the capital of France?"}], "outputs": ["Paris"]}
            ),
            scorers=[Safety()],
        )
        mock_evaluate.assert_called_once()


def test_input_is_optional_if_trace_is_provided():
    with mlflow.start_span() as span:
        span.set_inputs({"question": "What is the capital of France?"})
        span.set_outputs("Paris")

    trace = mlflow.get_trace(span.trace_id)

    with patch("mlflow.models.evaluate") as mock_evaluate:
        mlflow.genai.evaluate(
            data=pd.DataFrame({"trace": [trace]}),
            scorers=[Safety()],
        )

        mock_evaluate.assert_called_once()


# TODO: Remove this skip once databricks-agents 1.0 is released
@pytest.mark.skipif(
    os.getenv("GITHUB_ACTIONS") == "true",
    reason="Skipping test in CI because this test requires Agent SDK pre-release wheel",
)
@pytest.mark.parametrize("input_type", ["list", "pandas"])
def test_scorer_receives_correct_data_with_trace_data(input_type):
    sample_data = get_test_traces(type=input_type)
    received_args = []

    @scorer
    def dummy_scorer(inputs, outputs, expectations, trace):
        received_args.append((inputs, outputs, expectations, trace))
        return 0

    # Disable logging traces to MLflow to avoid calling mlflow APIs which need to be mocked
    with patch.dict("os.environ", {"AGENT_EVAL_LOG_TRACES_TO_MLFLOW_ENABLED": "false"}):
        mlflow.genai.evaluate(
            data=sample_data,
            scorers=[dummy_scorer],
        )

    inputs, outputs, expectations, trace = received_args[0]
    assert inputs == {"question": "What is MLflow?"}
    assert outputs == "I don't know"
    assert expectations == {
        "expected_response": "expected response for first question",
        "expected_facts": ["fact1", "fact2"],
        "guidelines": ["Be polite", "Be kind"],
        "my_custom_expectation": "custom expectation for the first question",
    }
    assert isinstance(trace, Trace)


@pytest.mark.parametrize("data_fixture", _ALL_DATA_FIXTURES)
def test_predict_fn_receives_correct_data(data_fixture, request):
    sample_data = request.getfixturevalue(data_fixture)

    received_args = []

    def predict_fn(question: str):
        received_args.append(question)
        return question

    @scorer
    def dummy_scorer(inputs, outputs):
        return 0

    mlflow.genai.evaluate(
        predict_fn=predict_fn,
        data=sample_data,
        scorers=[dummy_scorer],
    )

    received_args.pop(0)  # Remove the one-time prediction to check if a model is traced
    row_count = count_rows(sample_data)
    assert len(received_args) == row_count
    expected_contents = [
        "What is Spark?",
        "How can you minimize data shuffling in Spark?",
        "What is MLflow?",
    ][:row_count]
    # Using set because eval harness runs predict_fn in parallel
    assert set(received_args) == set(expected_contents)


def test_convert_scorer_to_legacy_metric():
    """Test that _convert_scorer_to_legacy_metric correctly sets _is_builtin_scorer attribute."""
    # Test with a built-in scorer
    builtin_scorer = Safety()
    legacy_metric = _convert_scorer_to_legacy_metric(builtin_scorer)

    # Verify the metric has the _is_builtin_scorer attribute set to True
    assert hasattr(legacy_metric, "_is_builtin_scorer")
    assert legacy_metric._is_builtin_scorer is True
    assert legacy_metric.name == builtin_scorer.name

    # Test with a custom scorer
    @scorer(name="custom_scorer")
    def custom_scorer_func(inputs, outputs=None, expectations=None, **kwargs):
        return {"score": 1.0}

    custom_scorer_instance = custom_scorer_func
    legacy_metric_custom = _convert_scorer_to_legacy_metric(custom_scorer_instance)

    # Verify the metric has the _is_builtin_scorer attribute set to False
    assert hasattr(legacy_metric_custom, "_is_builtin_scorer")
    assert legacy_metric_custom._is_builtin_scorer is False
    assert legacy_metric_custom.name == custom_scorer_instance.name
