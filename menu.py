import nuke

# Pixo has its own menu: an entry in the Nodes toolbar on the left and one in the menu bar at
# the top. The menu is built first and loads Pixo only when an item is used, so a problem inside
# Pixo shows up as a clear error when you click, never as a missing menu.
for bar in ("Nodes", "Nuke"):
    menu = nuke.menu(bar)
    if menu is not None and menu.findItem("Pixo") is None:
        # The Nodes toolbar shows only icons, so without one the menu is an invisible blank button.
        pixo_menu = menu.addMenu("Pixo", icon="Pixo.png")
        pixo_menu.addCommand("Timer", "from pixo import timer_ui; timer_ui.create()")
        pixo_menu.addCommand("Pulse", "from pixo import monitor_ui; monitor_ui.create()")
