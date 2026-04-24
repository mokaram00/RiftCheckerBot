# One-off: extract command_login_post_auth; run from project root then delete.
path = r"j:\Projects\RiftCheckerBot-main\commands.py"
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()

start = None
for i, line in enumerate(lines):
    if line.startswith("async def command_login(bot, message):"):
        start = i
        break
assert start is not None

body_start = None
for i in range(start, len(lines)):
    if lines[i].lstrip().startswith("# Slow endpoints:"):
        body_start = i
        break
assert body_start is not None

body_end = None
for i in range(body_start, len(lines)):
    if lines[i] == "    await epic_generator.kill()\n":
        j = i + 1
        while j < len(lines) and lines[j].strip() == "":
            j += 1
        if j < len(lines) and lines[j].startswith("async def command_theme"):
            body_end = i
            break
assert body_end is not None

body = lines[body_start : body_end + 1]

new_banner = """    bot.delete_message(msg.chat.id, msg.message_id)
    verb = "Rechecked" if recheck else "Logged in"
    msg = bot.send_message(
        message.chat.id,
        f"✅ {verb} account {account_data.get('displayName', 'HIDDEN_ID_ACCOUNT')}",
    )
"""
replaced = False
for i in range(len(body) - 1):
    if body[i].strip().startswith("bot.delete_message(msg.chat.id") and "Logged in account" in body[i + 1]:
        body[i : i + 2] = [new_banner]
        replaced = True
        break
if not replaced:
    raise SystemExit("Could not find delete+send banner block")

header = """async def command_login_post_auth(
    bot,
    message,
    user: RiftUser,
    user_data: dict,
    epic_generator: EpicGenerator,
    epic_user: EpicUser,
    msg,
    *,
    recheck: bool = False,
):
"""

new_func = [header] + body

call_line = (
    "    await command_login_post_auth(\n"
    "        bot, message, user, user_data, epic_generator, epic_user, msg\n"
    "    )\n"
)

out = lines[:body_start] + [call_line, "\n"] + lines[body_end + 1 :]

new_start = None
for i, line in enumerate(out):
    if line.startswith("async def command_login(bot, message):"):
        new_start = i
        break
assert new_start is not None

final = out[:new_start] + new_func + ["\n"] + out[new_start:]

with open(path, "w", encoding="utf-8") as f:
    f.writelines(final)

print("OK: extracted command_login_post_auth")
