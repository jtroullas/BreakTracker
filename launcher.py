import threading 
import time 
import webbrowser 
import uvicorn 
 
def _open_browser(): 
    time.sleep(1.5) 
    webbrowser.open("http://127.0.0.1:8000") 
 
if __name__ == "__main__": 
    threading.Thread(target=_open_browser, daemon=True).start() 
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False) 
