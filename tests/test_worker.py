import httpx

from app import worker


def test_classify_job_failure_does_not_retry_bad_input_or_preflight_errors():
    assert worker.classify_job_failure(ValueError("bad payload")) == (False, "invalid_data_or_configuration")
    assert worker.classify_job_failure(SystemExit("missing config")) == (False, "preflight_failed")


def test_classify_job_failure_retries_only_transient_http_statuses():
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    unavailable = httpx.HTTPStatusError("unavailable", request=request, response=httpx.Response(503, request=request))
    rejected = httpx.HTTPStatusError("bad request", request=request, response=httpx.Response(400, request=request))

    assert worker.classify_job_failure(unavailable) == (True, "http_503")
    assert worker.classify_job_failure(rejected) == (False, "http_400")


def test_attempt_count_is_non_negative_and_tolerates_bad_queue_data():
    assert worker._attempt_count({"attempts": "2"}) == 2
    assert worker._attempt_count({"attempts": -1}) == 0
    assert worker._attempt_count({"attempts": "not-a-number"}) == 0
