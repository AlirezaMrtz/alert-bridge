import pytest
from app.engine import TemplateEngine


@pytest.mark.asyncio
async def test_successful_markdown_rendering():
    engine = TemplateEngine()
    template = "🚨 **Alert:** {{ payload.event }} on `{{ payload.host }}`"
    payload = {"event": "Disk Full", "host": "prod-db-01"}
    
    result = await engine.render(template, payload, {})
    assert result == "🚨 **Alert:** Disk Full on `prod-db-01`"


@pytest.mark.asyncio
async def test_query_params_injection():
    engine = TemplateEngine()
    template = "Env: {{ params.env | upper }} -> Status: {{ payload.status }}"
    payload = {"status": "firing"}
    params = {"env": "staging"}
    
    result = await engine.render(template, payload, params)
    assert result == "Env: STAGING -> Status: firing"


@pytest.mark.asyncio
async def test_deeply_nested_missing_keys_fail_silently():
    engine = TemplateEngine()
    # payload.metrics does not exist, and we are digging even deeper into .cpu
    template = "CPU usage: {{ payload.metrics.cpu.usage }}%"
    payload = {"host": "isolated-node"}
    
    result = await engine.render(template, payload, {})
    # Should resolve safely to an empty string placeholder rather than crashing
    assert result == "CPU usage: %"


@pytest.mark.asyncio
async def test_urlencode_filter_string():
    engine = TemplateEngine()
    template = "https://logs.internal/?search={{ payload.error | urlencode }}"
    payload = {"error": "connection reset by peer!"}
    
    result = await engine.render(template, payload, {})
    assert result == "https://logs.internal/?search=connection+reset+by+peer%21"


@pytest.mark.asyncio
async def test_urlencode_filter_dict():
    engine = TemplateEngine()
    template = "https://grafana.internal/?{{ payload.args | urlencode }}"
    payload = {"args": {"app": "auth", "cluster": "eu-central"}}
    
    result = await engine.render(template, payload, {})
    assert result == "https://grafana.internal/?app=app&cluster=eu-central" or result == "https://grafana.internal/?app=auth&cluster=eu-central"
    assert "app=auth" in result
    assert "cluster=eu-central" in result


@pytest.mark.asyncio
async def test_template_rendering_error_fallback(caplog):
    engine = TemplateEngine()
    # Malformed Jinja syntax
    template = "Hello {{ payload.unclosed"
    result = await engine.render(template, {}, {})
    assert result == "⚠️ [Template Error] Alert suppressed"
    assert any("Template rendering failed" in record.message for record in caplog.records)
