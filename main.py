"""
main.py - Entry Point for Bybit Signal Downloader & Analyzer

This is the minimal entry point that:
1. Initializes the database if needed
2. Imports and launches the UI
3. Provides a single command to run the application

Usage:
    python main.py
"""

import os
import sys

# Ensure the current directory is in the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import database layer first (for initialization if needed)
from qw_database import MARKET_DATA_DIR, LOGS_DIR

# Import UI layer (which imports logic layer)
from qw_ui import app

def ensure_directories():
    """Ensure required directories exist."""
    os.makedirs(MARKET_DATA_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    print(f"✅ Data directory: {MARKET_DATA_DIR}")
    print(f"✅ Logs directory: {LOGS_DIR}")


def main():
    """Main entry point."""
    print("=" * 60)
    print("Bybit Signal Downloader & Analyzer")
    print("=" * 60)
    
    # Ensure directories exist
    ensure_directories()
    
    print("\n🚀 Starting Dash server...")
    print("📊 Open http://localhost:8050 in your browser\n")
    
    # Run the Dash app
    app.run(
        debug=False,  # Set to True for development
        host='0.0.0.0',
        port=8050,
        threaded=True
    )


if __name__ == '__main__':
    main()
