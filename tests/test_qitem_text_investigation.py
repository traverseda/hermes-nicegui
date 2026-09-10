"""Investigate what text content q-items get in the serialized element dict."""

import pytest
from nicegui.testing import User
from hermes_nicegui import web
from hermes_nicegui.plugins.sessions import SessionsPlugin


@pytest.fixture
def context(make_context):
    return make_context()


async def test_dump_session_row_qitems(user: User, context) -> None:
    """Dump every q-item that is a direct session row (has marker 'session-row'),
    plus any q-item with text set to anything suspicious."""
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions")
    await user.should_see("First session")

    client = user.client
    markers_map = {}
    for elem_id, elem in client.elements.items():
        for m in getattr(elem, '_markers', []) or []:
            markers_map.setdefault(m, []).append(elem_id)
    
    session_row_ids = markers_map.get('session-row', [])
    print(f"\n=== Session row q-item IDs: {session_row_ids} ===\n")
    
    for elem_id, elem in client.elements.items():
        tag = elem.tag
        text = getattr(elem, '_text', None)
        markers = getattr(elem, '_markers', []) or []
        if tag == "q-item" or (tag and "q-item" in tag):
            if elem_id in session_row_ids or (text and 'vue' in str(text).lower()) or markers:
                children = elem.default_slot.children if hasattr(elem, 'default_slot') else []
                slot_names = list(getattr(elem, 'slots', {}).keys()) if hasattr(elem, 'slots') else []
                print(f"\n--- q-item {elem_id} markers={markers} ---")
                print(f"  tag={tag}")
                print(f"  text={repr(text)}")
                print(f"  children={[(c.id, c.tag, repr(getattr(c, '_text', None))) for c in children]}")
                print(f"  slot_names={slot_names}")
