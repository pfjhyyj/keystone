"""Render neutral content as Feishu message bodies, shared by group bots and apps.

Rendering is deterministic and uses plain text, so mentions or markdown in user content
are never interpreted.
"""


def card(content):
    elements = []
    if content.get("body"):
        elements.append({"tag": "div", "text": {"tag": "plain_text", "content": content["body"]}})
    if content.get("fields"):
        elements.append(
            {
                "tag": "div",
                "fields": [
                    {
                        "is_short": True,
                        "text": {
                            "tag": "plain_text",
                            "content": f"{field['label']}：{field['value']}",
                        },
                    }
                    for field in content["fields"]
                ],
            }
        )
    if content.get("links"):
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": link["text"]},
                        "type": "default",
                        "url": link["url"],
                    }
                    for link in content["links"]
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": content["title"]}},
        "elements": elements,
    }


def _text(content):
    return {"tag": "div", "text": {"tag": "plain_text", "content": content}}


def _row(buttons):
    return {
        "tag": "column_set",
        "flex_mode": "flow",
        "columns": [{"tag": "column", "width": "auto", "elements": [button]} for button in buttons],
    }


def action_card(content, delivery_id, outcome=None):
    """A JSON 2.0 shared card whose buttons call back with the delivery ID and action key.

    Feishu does not state that JSON 1.0 buttons raise the current callback, and a card's
    version cannot change on update. Shared (update_multi) cards can later be updated by
    message ID for everyone. With an outcome, the buttons are replaced by that line.
    """
    elements = []
    if content.get("body"):
        elements.append(_text(content["body"]))
    elements += [_text(f"{field['label']}：{field['value']}") for field in content["fields"]]
    if content.get("links"):
        elements.append(
            _row(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": link["text"]},
                    "type": "default",
                    "behaviors": [{"type": "open_url", "default_url": link["url"]}],
                }
                for link in content["links"]
            )
        )
    if outcome is not None:
        elements.append(_text(outcome))
    else:
        elements.append(
            _row(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": action["text"]},
                    "type": "danger" if action["style"] == "danger" else action["style"],
                    "behaviors": [
                        {
                            "type": "callback",
                            "value": {"delivery_id": str(delivery_id), "action": action["key"]},
                        }
                    ],
                }
                for action in content["actions"]
            )
        )
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": content["title"]}},
        "body": {"elements": elements},
    }


def render(content, delivery_id=None):
    """Return (msg_type, content object) for a validated neutral content."""
    if content["kind"] == "text":
        return "text", {"text": content["text"]}
    if content.get("actions"):
        return "interactive", action_card(content, delivery_id)
    return "interactive", card(content)
