"""Render the idle stat-screen images for the README (real stats, mock status chip)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import claude_screen as cs

cs._STATS = cs.compute_stats()
cs._CLAUDE_STATUS = {"indicator": "none", "description": "All Systems Operational"}

cs.render_stats_overview(0.4, cs._STATS).save("docs/screen-activity.png")
cs.render_stats_models(0.4, cs._STATS).save("docs/screen-models.png")
print("wrote docs/screen-activity.png and docs/screen-models.png")
