# Inside your existing telegram_engine.py, add this handler logic:

from risk_manager import handle_risk_command
from database import get_last_alert_data # You need to ensure this function exists in database.py

def on_telegram_message(update, context):
    text = update.message.text
    if text.startswith("/risk"):
        try:
            risk_amt = float(text.split(" ")[1])
            symbol = update.message.reply_to_message.text.split("Stock: <b>")[1].split("</b>")[0]
            
            # Fetch the stored ATR stop from your database
            alert_data = get_last_alert_data(symbol) 
            
            response = handle_risk_command(
                symbol=symbol, 
                stop_loss=alert_data['atr_stop'], 
                risk_amount=risk_amt, 
                price=alert_data['price']
            )
            context.bot.send_message(chat_id=update.effective_chat.id, text=response, parse_mode='HTML')
        except Exception as e:
            context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Usage: Reply to an alert with /risk <amount>")
