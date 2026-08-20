import requests, json
r = requests.get('https://api.telegram.org/bot8854681783:AAGwaZP1QCLSbNc9j8I3W7G_iS1xUXbUW7I/getUpdates')
data = r.json()
print('CHAT_ID_START')
for update in data.get('result', []):
    msg = update.get('message', {})
    chat = msg.get('chat', {})
    print(f'chat_id: {chat.get("id")} | username: {chat.get("username", "")} | first_name: {chat.get("first_name", "")}')
if not data.get('result'):
    print('No messages found - did you send /start to the bot?')
print('CHAT_ID_END')
