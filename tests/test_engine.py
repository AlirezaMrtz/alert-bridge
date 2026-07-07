from app.engine import TemplateEngine


def test_successful_markdown_rendering():
    engine = TemplateEngine()
    template = "🚨 **Alert:** {{ payload.event }} on `{{ payload.host }}`"
    payload = {"event": "Disk Full", "host": "prod-db-01"}
    
    result = engine.render(template, payload, {})
    assert result == "🚨 **Alert:** Disk Full on `prod-db-01`"


def test_query_params_injection():
    engine = TemplateEngine()
    template = "Env: {{ params.env | upper }} -> Status: {{ payload.status }}"
    payload = {"status": "firing"}
    params = {"env": "staging"}
    
    result = engine.render(template, payload, params)
    assert result == "Env: STAGING -> Status: firing"


def test_deeply_nested_missing_keys_fail_silently():
    engine = TemplateEngine()
    # payload.metrics does not exist, and we are digging even deeper into .cpu
    template = "CPU usage: {{ payload.metrics.cpu.usage }}%"
    payload = {"host": "isolated-node"}
    
    result = engine.render(template, payload, {})
    # Should resolve safely to an empty string placeholder rather than crashing
    assert result == "CPU usage: %"


def test_urlencode_filter_string():
    engine = TemplateEngine()
    template = "https://logs.internal/?search={{ payload.error | urlencode }}"
    payload = {"error": "connection reset by peer!"}
    
    result = engine.render(template, payload, {})
    assert result == "https://logs.internal/?search=connection+reset+by+peer%21"


def test_urlencode_filter_dict():
    engine = TemplateEngine()
    template = "https://grafana.internal/?{{ payload.args | urlencode }}"
    payload = {"args": {"app": "auth", "cluster": "eu-central"}}
    
    result = engine.render(template, payload, {})
    assert result == "https://grafana.internal/?app=auth&cluster=eu-central"
