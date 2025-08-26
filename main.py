import requests, pytz, os, time
from dateutil import parser
from datetime import datetime
from telegram import Bot
from config import get_db_connection
from dotenv import load_dotenv
load_dotenv()

# Config
TELEGRAM_TOKEN =  os.getenv('TELEGRAM_TOKEN')
GROUP_CHAT_ID = int(os.getenv('GROUP_CHAT_ID'))
CMC_API_KEY = os.getenv('CMC_API_KEY')
CMC_API = os.getenv('CMC_API')
CP_API_KEY = os.getenv('CP_API_KEY')
CP_API = os.getenv('CP_API')
TARGET_SYMBOLS = os.getenv("TARGET_COINS", "").split(",")
CHANGES_THRESHOLD = os.getenv('CHANGES_THRESHOLD')
CHANGES_CATEGORY = os.getenv('CHANGES_CATEGORY')
HEADERS = {
    "Accepts": "application/json",
    "X-CMC_PRO_API_KEY": CMC_API_KEY
}


bot = Bot(token=TELEGRAM_TOKEN)

#NEWS
def get_latest_cryptopanic_news(target_symbols):
    currencies = ",".join(symbol.upper() for symbol in target_symbols)
    url = f'{CP_API}/posts/'
    params = {
        "auth_token": CP_API_KEY,
        "currencies": currencies,
        "kind": "news",  
        "public": "true"
    }
    try:
        response = requests.get(url, params=params)
        print(url)
        response.raise_for_status()
        news = response.json().get("results", [])[:5]  # limit to 5 latest
        return news
    except Exception as e:
        print("Error fetching CP news:", e)
        return []

def get_latest_cmc_news():
    url = f'{CMC_API}/news/latest'
    params = {
        "limit": 5 
    }

    try:
        response = requests.get(url, headers=HEADERS, params=params)
        response.raise_for_status()
        return response.json().get("data", [])
    except Exception as e:
        print("Error fetching CMC news:", e)
        return []

def filter_cmc_news(news, symbols, keywords=None):
    keywords = keywords or []
    filtered = []

    for article in news:
        title = article.get("title", "").lower()
        summary = article.get("body", "").lower()  # if available

        if any(sym.lower() in title or sym.lower() in summary for sym in symbols) \
           or any(kw.lower() in title or kw.lower() in summary for kw in keywords):
            filtered.append(article)
    
    return filtered

#ALERT
def get_all_crypto():
    url = f'{CMC_API}/cryptocurrency/listings/latest'
    params = {
        'start': '1',
        'limit': '100',   
        'convert': 'IDR'
    }
    response = requests.get(url, params=params, headers=HEADERS)
    response.raise_for_status()
    print('API hit')
    return response.json()

def extract_target_price(data , target_coin):
    sorted_data = sorting_coin(data, target_coin)
    prices = [coin["quote"]["IDR"]["price"] for coin in sorted_data]
    timestamps = [coin["last_updated"] for coin in sorted_data]
    latest_timestamp = max(timestamps)
    latest_timestamp = convert_time(latest_timestamp)
    return prices, latest_timestamp

def extract_target_info(data, target_coin):
    sorted_data = sorting_coin(data, target_coin)
    info = []
    for coin in sorted_data:
        coin_info = {
            "symbol": coin["symbol"],
            "name": coin["name"],
            "percent_change_1h": coin["quote"]["IDR"].get("percent_change_1h"),
            "percent_change_24h": coin["quote"]["IDR"].get("percent_change_24h"),
            "market_cap": coin["quote"]["IDR"].get("market_cap"),
            "volume_24h": coin["quote"]["IDR"].get("volume_24h"),
            "last_updated": convert_time(coin["last_updated"])
        }
        info.append(coin_info)
    return info

def check_target_changes(changes_cat, raw_data, target_symbols):
    conn = get_db_connection()
    try:
        current_prices, _ = extract_target_price(raw_data, target_symbols)
        coin_info = extract_target_info(raw_data, target_symbols)
        usd_to_idr = get_usd_to_idr_rate()


        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT main, alt_1, alt_2, alt_3, alt_4, alt_5 
                FROM cmc_alert
                WHERE changes_cat = %s
            """, (changes_cat,))
            result = cursor.fetchone()

            if not result:
                print(f"No historical data found for category {changes_cat}")
                return

            threshold = float(CHANGES_THRESHOLD)

            for i in range(len(current_prices)):
                past_price = result[i]
                current_price = current_prices[i]

                if past_price == 0:
                    continue  # Skip comparison if there's no data

                change = ((current_price - past_price) / past_price) * 100
                usd_price = current_price/usd_to_idr

                if abs(change) >= threshold:
                    coin = coin_info[i]
                    send_coin_alert_message(
                        coin_name=coin["name"],
                        symbol=coin["symbol"],
                        price=current_price,
                        usd_price=usd_price,
                        change_1h=coin["percent_change_1h"],
                        change_24h=coin["percent_change_24h"],
                        interval_change=change,
                        last_updated=coin["last_updated"],
                        interval=changes_cat
                    )
    except Exception as e:
        print("Error checking changes:", e)
    finally:
        conn.close()

def save_to_db(prices, category, last_updated, now):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            sql = """
                UPDATE cmc_alert
                SET
                    main = %s,
                    alt_1 = %s,
                    alt_2 = %s,
                    alt_3 = %s,
                    alt_4 = %s,
                    alt_5 = %s,
                    symbols = %s,
                    last_updated = %s,
                    timestamp = %s
                WHERE
                    changes_cat = %s
            """
            cursor.execute(sql, (
                prices[0], prices[1], prices[2], prices[3], prices[4], prices[5],
                ",".join(TARGET_SYMBOLS),
                last_updated,
                now,
                category
            ))
        conn.commit()
    finally:
        conn.close()

def send_hourly_coin_summary(data, target_symbols):
    try:
        prices, _ = extract_target_price(data, target_symbols)
        coin_info = extract_target_info(data, target_symbols)
        message = format_hourly_coin_summary(prices, coin_info)
        bot.send_message(chat_id=GROUP_CHAT_ID, text=message, parse_mode='Markdown')
        print("Hourly sent.")

    except Exception as e:
        print("Error sending hourly:", e)



def run_price_monitor():
    last_news_hour = -1  

    while True:
        now = datetime.now()
        minute = now.minute
        hour = now.hour

        # Only run logic every 5 minutes
        if minute % 5 == 0:
            try:
                data = get_all_crypto()
                prices, last_updated = extract_target_price(data, TARGET_SYMBOLS)

                # Always save & check 5m
                check_target_changes("5m", data, TARGET_SYMBOLS)
                save_to_db(prices, "5m", last_updated, now)
                print("5m saved and checked")

                # Prioritized saving: 30m > 15m > 10m
                if minute % 30 == 0:
                    save_to_db(prices, "30m", last_updated, now)
                    print("30m saved")

                elif minute % 15 == 0:
                    save_to_db(prices, "15m", last_updated, now)
                    print("15m saved")

                elif minute % 10 == 0:
                    save_to_db(prices, "10m", last_updated, now)
                    print("10m saved")

                # Hourly summary
                if minute == 0:
                    send_hourly_coin_summary(data, TARGET_SYMBOLS)

                    if hour % 8 == 0 and hour != last_news_hour:
                        panic_news = get_latest_cryptopanic_news(TARGET_SYMBOLS)
                        # cmc_news = get_latest_cmc_news()
                        # filtered_cmc_news = filter_cmc_news(cmc_news)
                        send_news(panic_news, '')
                        last_news_hour = hour


            except Exception as e:
                print("Error occurred:", e)

            # Avoid sleeping full 5m if you're syncing exactly on minute boundaries
            time.sleep(60)

        else:
            # Sleep until the next minute
            time.sleep(60 - datetime.now().second)


#Other utilities
def convert_time(timestamp):
    dt_utc = parser.isoparse(timestamp)
    jakarta_tz = pytz.timezone("Asia/Jakarta")
    dt_jakarta = dt_utc.astimezone(jakarta_tz)
    timestamp = dt_jakarta.strftime("%d-%m-%Y %H:%M:%S WIB")
    return timestamp

def sorting_coin(data, target_coin):
    target_symbols = [sym.strip().upper() for sym in target_coin]
    filtered_data = [coin for coin in data["data"] if coin["symbol"] in target_symbols]
    sorted_data = sorted(filtered_data, key=lambda x: target_symbols.index(x["symbol"]))
    return sorted_data

def get_usd_to_idr_rate():
    url = 'https://open.er-api.com/v6/latest/USD'
    response = requests.get(url)
    data = response.json()
    return data['rates']['IDR']

def format_number(number):
    abs_number = abs(number)
    if abs_number >= 1_000_000_000_000_000:
        return f'{number / 1_000_000_000_000_000:.2f}Q'
    elif abs_number >= 1_000_000_000_000:
        return f'{number / 1_000_000_000_000:.2f}T'
    elif abs_number >= 1_000_000_000:
        return f'{number / 1_000_000_000:.2f}B'
    elif abs_number >= 1_000_000:
        return f'{number / 1_000_000:.2f}M'
    elif abs_number >= 1_000:
        return f'{number / 1_000:.2f}K'
    else:
        return f'{number:.0f}'

#Telegram Bot
def send_coin_alert_message(coin_name, symbol, price, usd_price, change_1h, change_24h, interval_change, last_updated, interval):
    emoji_1h = "📈" if change_1h >= 0 else "📉"
    emoji_24h = "📈" if change_24h >= 0 else "📉"
    message = f"""
🚨 *{coin_name} ({symbol}) Alert!*

💰 *Current Price:* Rp {price:,.4f}
💰 *Current Price:* $ {usd_price:,.4f}

🔄 *Interval Change ({interval}):* {interval_change:+.2f}%

{emoji_1h} *1h Change:* {change_1h:+.2f}%
{emoji_24h} *24h Change:* {change_24h:+.2f}%
🕒 *Last Updated:* {last_updated}
    """
    bot.send_message(chat_id=GROUP_CHAT_ID, text=message, parse_mode='Markdown')

def format_hourly_coin_summary(prices, coin_info):
    usdtoidr = get_usd_to_idr_rate()
    message_lines = ["🕒 *Hourly Coin Summary*\n"]
    for i, coin in enumerate(coin_info):
        usd_price = prices[i]/usdtoidr
        change_1h = coin['percent_change_1h']
        change_24h = coin['percent_change_24h']
        emoji_1h = "📈" if change_1h >= 0 else "📉"
        emoji_24h = "📈" if change_24h >= 0 else "📉"
        line = f"""
*{coin['name']} ({coin['symbol']})*
💰 Price: Rp {prices[i]:,.4f}
💰 Price: $ {usd_price:,.4f}
{emoji_1h} 1h Change: {coin['percent_change_1h']:+.2f}%
{emoji_24h} 24h Change: {coin['percent_change_24h']:+.2f}%
🕓 Updated: {coin['last_updated']}
"""
        message_lines.append(line)

    return "\n".join(message_lines)

def send_news(crypto_panic_news, cmc_news):
    message = "📰 *Latest Crypto News Roundup*\n\n"

    if crypto_panic_news:
        message += "🔥 _From CryptoPanic:_\n"
        for article in crypto_panic_news:
            title = article.get("title", "No Title")
            url = article.get("url", "")
            source = article.get("source", {}).get("title", "")
            published_at = article.get("published_at", "")[:16].replace("T", " ")
            description = article.get("description", "")

            if url:
                message += f"• [{title}]({url})\n"
            else:
                message += f"• {title}\n"

            if description:
                message += f"`{description}`\n"

            message += f"_🗞 {source} — 🕒 {published_at}_\n\n"

    if cmc_news:
        message += "🗞 _From CoinMarketCap:_\n"
        for article in cmc_news:
            title = article.get("title", "No Title")
            url = article.get("url", "")
            published_at = article.get("created_at", "")[:16].replace("T", " ")
            message += f"• [{title}]({url})\n"
            message += f"_🕒 {published_at}_\n\n"
    
    bot.send_message(chat_id=GROUP_CHAT_ID, text=message, parse_mode='Markdown')


def main():
    run_price_monitor()

if __name__ == '__main__':
    main()