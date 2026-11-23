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


def render(content):
    """Return (msg_type, content object) for a validated neutral content."""
    if content["kind"] == "text":
        return "text", {"text": content["text"]}
    return "interactive", card(content)
