@match_event(host="bar", level=(LEVEL.CRIT, LEVEL.WARN))
def on_event(event) :
    event.customfiled = "X"
    notify(event, user="mdevaev", wait=2)
    notify(event, user="andozer", wait=2)
    notify(event, user="nbryskin")

