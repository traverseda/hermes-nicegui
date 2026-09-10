"""Investigation of build artifacts in session list q-item text content."""

import pytest
from nicegui.testing import User
from hermes_nicegui import web
from hermes_nicegui.plugins.sessions import SessionsPlugin


@pytest.fixture
def context(make_context):
    return make_context()


async def test_q_item_text_no_artifacts(user: User, context) -> None:
    """Verify that no q-item in the sessions list has build artifact strings
    as text content. These would appear as raw template/JS strings like:
      - 'import * as Vue from "vue"'
      - 'globalThis.Vue = Vue'
      - 'document.getElementById("esm-fallback")?.remove()'
    """
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions")
    await user.should_see("First session")

    client = user.client
    for elem_id, elem in client.elements.items():
        tag = elem.tag
        text = getattr(elem, '_text', None)
        if tag == 'q-item' and text:
            for artifact in [
                'import * as Vue from "vue"',
                'globalThis.Vue = Vue',
                'esm-fallback',
                'document.getElementById',
                'from "vue"',
            ]:
                if artifact in (text or ''):
                    raise AssertionError(
                        f"q-item {elem_id} has build artifact in text: {repr(text)}"
                    )
