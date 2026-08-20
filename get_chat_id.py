import requests, json
r = requests.get('https://api.telegram.org/bot8854681783:AAGwaZP1QCLSbNc9j8I3W7G_iS1xUXbUW7I/getMe')
print('BOT_INFO_START')
print(r.text)
print('BOT_INFO_END')
