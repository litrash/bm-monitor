import requests
r = requests.get('https://api.telegram.org/bot8854681783:AAGwaZP1QCLSbNc9j8I3W7G_iS1xUXbUW7I/getUpdates')
print('CHAT_START')
print(r.text)
print('CHAT_END')
