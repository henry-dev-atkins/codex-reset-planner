"""Windows double-click entrypoint; uses pythonw.exe if Python has .pyw associations."""
from app import ResetApp

if __name__ == '__main__':
    ResetApp().run()
