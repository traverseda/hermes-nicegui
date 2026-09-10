"""Render the HTML and check for build artifacts in session row q-items."""

import pytest
from nicegui.testing import User
from hermes_nicegui import web
from hermes_nicegui.plugins.sessions import SessionsPlugin


@pytest.fixture
def context(make_context):
    return make_context()


async def test_check_rendered_html(user: User, context) -> None:
    """Render HTML and check for build artifacts."""
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions")
    await user.should_see("First session")

    # Get the rendered HTML from the page
    html = user.page.html if hasattr(user, 'page') and hasattr(user.page, 'html') else None
    
    # Also check the Client's layout for rendered content
    client = user.client
    layout_html = str(client.layout) if client.layout else None
    
    print(f"\n=== Layout HTML (first 3000 chars) ===\n{layout_html[:3000] if layout_html else 'N/A'}\n")
    
    # Check for build artifacts in rendered text
    artifacts = ['import * as Vue', 'globalThis.Vue', 'esm-fallback', 
                 'document.getElementById', 'from "vue"']
    
    all_text = ""
    for elem_id, elem in client.elements.items():
        text = getattr(elem, '_text', None)
        if text:
            all_text += text + "\n"
    
    for a in artifacts:
        if a in all_text:
            print(f"FOUND artifact '{a}' in element text!\n")
