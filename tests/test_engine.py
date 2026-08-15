import pytest
from app.engine import TemplateEngine


@pytest.mark.asyncio
async def test_successful_markdown_rendering():
    engine = TemplateEngine()
    template = "🚨 **Alert:** {{ payload.event }} on `{{ payload.host }}`"
    payload = {"event": "Disk Full", "host": "prod-db-01"}
    
    success, result = await engine.render(template, payload, {})
    assert success is True
    assert result == "🚨 **Alert:** Disk Full on `prod-db-01`"


@pytest.mark.asyncio
async def test_query_params_injection():
    engine = TemplateEngine()
    template = "Env: {{ params.env | upper }} -> Status: {{ payload.status }}"
    payload = {"status": "firing"}
    params = {"env": "staging"}
    
    success, result = await engine.render(template, payload, params)
    assert success is True
    assert result == "Env: STAGING -> Status: firing"


@pytest.mark.asyncio
async def test_deeply_nested_missing_keys_fail_silently():
    engine = TemplateEngine()
    # payload.metrics does not exist, and we are digging even deeper into .cpu
    template = "CPU usage: {{ payload.metrics.cpu.usage }}%"
    payload = {"host": "isolated-node"}
    
    success, result = await engine.render(template, payload, {})
    # Should resolve safely to an empty string placeholder rather than crashing
    assert success is True
    assert result == "CPU usage: %"


@pytest.mark.asyncio
async def test_urlencode_filter_string():
    engine = TemplateEngine()
    template = "https://logs.internal/?search={{ payload.error | urlencode }}"
    payload = {"error": "connection reset by peer!"}
    
    success, result = await engine.render(template, payload, {})
    assert success is True
    assert result == "https://logs.internal/?search=connection%20reset%20by%20peer%21"


@pytest.mark.asyncio
async def test_urlencode_filter_dict():
    engine = TemplateEngine()
    template = "https://grafana.internal/?{{ payload.args | urlencode }}"
    payload = {"args": {"app": "auth service", "cluster": "eu-central"}}
    
    success, result = await engine.render(template, payload, {})
    assert success is True
    assert "app=auth%20service" in result
    assert "cluster=eu-central" in result
    assert "+" not in result


@pytest.mark.asyncio
async def test_template_rendering_error_fallback(caplog):
    engine = TemplateEngine()
    # Malformed Jinja syntax
    template = "Hello {{ payload.unclosed"
    success, result = await engine.render(template, {}, {})
    assert success is False
    assert "Template rendering failed" in result
    assert any("Template rendering failed" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_sandboxed_environment_blocks_rce_probes():
    engine = TemplateEngine()
    # Unsafe attribute access resolves to empty string without exposing classes
    success, result = await engine.render("{{ payload.__class__ }}", {}, {})
    assert result == ""
    
    success, result = await engine.render("{{ [].__class__.__base__ }}", {}, {})
    assert result == ""

    # Invoking unsafe methods/subclasses raises SecurityError caught by render
    success, result = await engine.render("{{ ''.__class__.__mro__[1].__subclasses__() }}", {}, {})
    assert success is False
    assert "access to attribute '__class__'" in result
