import aiohttp
import asyncio
import json
from twitchio.ext import commands, routines

try:
    with open('config.json', 'r', encoding='utf-8') as file:
        config = json.load(file)
        TWITCH_TOKEN = config['TWITCH_TOKEN']
except FileNotFoundError:
    print("FEHLER: Die Datei 'config.json' wurde nicht gefunden!")
    exit()
except KeyError:
    print("FEHLER: 'TWITCH_TOKEN' wurde in der config.json nicht gefunden!")
    exit()

CHANNEL_NAME = 'dawasteh'
BOT_NAME = 'dawastehbot'
LLM_SERVER_URL = 'http://127.0.0.1:1235/v1/chat/completions'

CURRENT_GAME = "Dein aktuelles Spiel" 
STREAM_TITLE = "Dein Stream Titel"

class PandaBot(commands.Bot):

    def __init__(self) -> None:
        super().__init__(token=TWITCH_TOKEN, prefix='!', initial_channels=[CHANNEL_NAME])  # type: ignore
        
        self.chat_history: list[str] = []
        self.last_message_time: float = asyncio.get_event_loop().time()
        self.is_processing: bool = False

    async def event_ready(self) -> None:
        print(f'PandaBot ({BOT_NAME}) ist online!')
        print(f'Verbunden mit Kanal | {CHANNEL_NAME}')
        self.random_chat_routine.start()  # pyright: ignore[reportAttributeAccessIssue]

    async def event_message(self, message) -> None:
        if message.echo:
            self.chat_history.append(f"{BOT_NAME}: {message.content}")
            if len(self.chat_history) > 10:
                self.chat_history.pop(0)
            return

        if not message.author:
            return

        self.last_message_time = asyncio.get_event_loop().time()
        
        self.chat_history.append(f"{message.author.name}: {message.content}")
        if len(self.chat_history) > 10:
            self.chat_history.pop(0)

        msg_lower = message.content.lower()
        mentions = ["pandabot", f"@{BOT_NAME}"]
        
        if any(mention in msg_lower for mention in mentions):
            if not self.is_processing: # Nur antworten, wenn er nicht gerade rechnet
                await self.respond_with_llm(message.channel, context=message.content, user=message.author.name)
            else:
                print("Bot denkt bereits nach, ignoriere Spam...")

    async def respond_with_llm(self, channel, context: str = "", user: str = "", is_random: bool = False) -> None:
        self.is_processing = True 

        system_prompt = (
            "Du bist PandaBot, ein freundlicher, witziger und kurzer Chatbot im Twitch-Stream von dawasteh. "
            f"Der Streamer spielt gerade {CURRENT_GAME} und der Titel ist '{STREAM_TITLE}'. "
            "Antworte in ein bis zwei kurzen Sätzen auf Deutsch. Sei unterhaltsam!"
        )

        chat_context = "\n".join(self.chat_history[-5:])
        
        if is_random:
            user_prompt = f"Der Chat ist gerade ruhig. Hier sind die letzten Nachrichten:\n{chat_context}\nSchreibe etwas Passendes zum Stream oder Spiel, um die Leute zu unterhalten!"
        else:
            user_prompt = f"Hier ist der aktuelle Chatverlauf:\n{chat_context}\n\n{user} hat gerade zu dir gesagt: '{context}'. Antworte direkt auf {user}."

        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.8,
            "max_tokens": 70
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(LLM_SERVER_URL, json=payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        reply = data['choices'][0]['message']['content'].strip()
                        await channel.send(reply)
                    else:
                        print(f"Fehler vom Llama-Server: {response.status}")
        except Exception as e:
            print(f"Konnte den Llama-Server nicht erreichen: {e}")
        finally:
            self.is_processing = False 

    @routines.routine(seconds=60)  # type: ignore
    async def random_chat_routine(self) -> None:
        current_time = asyncio.get_event_loop().time()
        
        if current_time - self.last_message_time > 180 and not self.is_processing:
            channels = getattr(self, "connected_channels", [])
            channel = next((c for c in channels if c.name == CHANNEL_NAME), None)
            
            if channel:
                print("Chat ist ruhig, PandaBot wird aktiv...")
                await self.respond_with_llm(channel, is_random=True)
                self.last_message_time = asyncio.get_event_loop().time()

if __name__ == "__main__":
    bot = PandaBot()
    bot.run()
