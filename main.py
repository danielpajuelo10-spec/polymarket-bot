"""Entry point for deployment (Procfile: worker: python main.py)."""
import runpy, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
runpy.run_path("watchdog.py", run_name="__main__")
