"""Mock 'State Board Entrance Exam' portal: deliberately messy, gov-site style.

Run: python portal/app.py  (serves on :5050)
Flow: landing (popups) -> personal details -> OTP -> centre -> CAPTCHA -> review -> fee payment -> receipt
The OTP "SMS" shows up at /__sms (the demo UI shows it as the user's phone).

Applications and payments live server-side (APPS / LEDGER), like a real portal, so losing the browser session
does not undo a payment. Like many real portals it will happily charge twice if asked to.
Fault injection: /__fault?mode=expire_after_pay -> the next payment is taken, then the session dies before the
receipt is shown. Logging back in (/resume) lands on a Payment Status page the agent has never seen.
/__ledger lists every charge the portal accepted (ground truth for "no duplicated submission").
"""
from __future__ import annotations

import os
import random
import string
import time

from flask import Flask, jsonify, redirect, render_template_string, request, session, url_for

app = Flask(__name__)
app.secret_key = "formpilot-demo"

SMS_INBOX: list[dict] = []
APPS: dict[str, dict] = {}   # application id -> server-side record (survives session loss)
LEDGER: list[dict] = []      # every charge the portal accepted
FAULT = {"mode": os.getenv("PORTAL_FAULT", "")}
CITIES = {
    "Karnataka": ["Bengaluru", "Mysuru", "Mangaluru", "Hubballi"],
    "Maharashtra": ["Mumbai", "Pune", "Nagpur"],
    "Tamil Nadu": ["Chennai", "Coimbatore", "Madurai"],
}
CENTRES = {
    "Bengaluru": ["PES University, Banashankari (C-101)", "RV College, Mysore Rd (C-102)", "Christ University, Hosur Rd (C-103)"],
    "Mysuru": ["JSS Science & Tech Univ (C-201)", "NIE Mysuru (C-202)"],
    "Mangaluru": ["NITK Surathkal (C-301)"],
    "Hubballi": ["KLE Tech Univ (C-401)"],
    "Mumbai": ["IIT Bombay (C-501)", "VJTI Matunga (C-502)"],
    "Pune": ["COEP Shivajinagar (C-601)"],
    "Nagpur": ["VNIT Nagpur (C-701)"],
    "Chennai": ["Anna University (C-801)", "SRM Kattankulathur (C-802)"],
    "Coimbatore": ["PSG Tech (C-901)"],
    "Madurai": ["Thiagarajar College (C-951)"],
}

BASE = """<!doctype html><html><head><meta charset="utf-8"><title>SBEE-2026 Candidate Portal</title>
<style>
body{font-family:Verdana,Arial,sans-serif;font-size:13px;background:#e8e4d8;margin:0;color:#222}
.top{background:#7a1f1f;color:#fff;padding:6px 14px;font-size:11px}
.hdr{background:linear-gradient(#fff6c8,#f1d97a);border-bottom:3px solid #7a1f1f;padding:10px 14px}
.hdr h1{margin:0;font-size:20px;color:#7a1f1f}.hdr small{color:#555}
marquee{background:#fff3b0;border:1px solid #cc0;padding:3px;font-weight:bold;color:#b00}
.wrap{width:760px;margin:12px auto;background:#fff;border:1px solid #999;padding:14px}
.steps{font-size:11px;color:#777;margin-bottom:8px}.steps b{color:#7a1f1f}
table.f td{padding:5px 6px;vertical-align:top}table.f td.l{width:210px;font-weight:bold}
input[type=text],input[type=password],select{border:1px solid #888;padding:3px;font-size:12px;min-width:200px}
.req{color:red}.err{color:#b00;background:#fee;border:1px solid #b00;padding:5px;margin-bottom:8px}
.btn{background:#7a1f1f;color:#fff;border:0;padding:6px 16px;font-size:13px;cursor:pointer}
.btn2{background:#ddd;color:#333;border:1px solid #888;padding:6px 14px;cursor:pointer}
a{color:#0033cc}.tiny{font-size:10px}
#cookie{position:fixed;bottom:0;left:0;right:0;background:#222;color:#fff;padding:10px 20px;z-index:50}
#ad{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.55);z-index:60;display:flex;align-items:center;justify-content:center}
#ad div{background:#fff;padding:22px;width:360px;text-align:center;border:3px solid #7a1f1f}
#ad .x{font-size:9px;color:#999;cursor:pointer;display:block;text-align:right}
</style></head><body>
<div class="top">Government of Karnataka | Skip to main content | A- A A+ | हिन्दी | ಕನ್ನಡ</div>
<div class="hdr"><h1>State Board Entrance Examination (SBEE) 2026</h1><small>Official Candidate Services Portal &mdash; Best viewed in IE 8 at 1024x768</small></div>
<marquee>Last date for registration extended to 30-NOV-2026 &bull; Beware of fraudulent websites &bull; Helpline 080-2222-0000</marquee>
<div class="wrap">{{ body|safe }}</div>
{{ extra|safe }}
</body></html>"""


def page(body: str, extra: str = "", **ctx):
    body = render_template_string(body, **ctx)
    return render_template_string(BASE, body=body, extra=extra)


def steps(n: int) -> str:
    names = ["Personal", "Verify Mobile", "Exam Centre", "Security Check", "Review", "Fee", "Receipt"]
    return '<div class="steps">' + " &gt; ".join(f"<b>{s}</b>" if i == n else s for i, s in enumerate(names)) + "</div>"


@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    return r


@app.route("/__sms")
def sms():
    return jsonify(SMS_INBOX[-5:])


@app.route("/__reset", methods=["POST", "GET"])
def reset():
    SMS_INBOX.clear()
    APPS.clear()
    LEDGER.clear()
    FAULT["mode"] = ""
    session.clear()
    return redirect("/")


@app.route("/__ledger")
def ledger():
    return jsonify({"count": len(LEDGER), "charges": LEDGER, "fault": FAULT["mode"]})


@app.route("/__fault")
def fault():
    FAULT["mode"] = request.args.get("mode", "")
    return jsonify(FAULT)


def _app():
    return APPS.get(session.get("app_id"))


@app.before_request
def stale_session():
    """A session the server has expired cannot be revived by replaying an old cookie."""
    if request.path.startswith("/__") or request.endpoint in (None, "landing", "dead_end", "register", "expired", "resume", "captcha_svg"):
        return None
    a = _app()
    if a and session.get("epoch") != a["epoch"]:
        session.clear()
        return redirect("/expired")
    return None


def _charge(a: dict, fee: int, upi: str) -> dict:
    t = {"txn": "TXN" + "".join(random.choices(string.digits, k=8)), "amount": fee + 18, "status": "success", "ts": time.time()}
    a["txns"].append(t)
    a["upi"] = upi
    a["paid"] = True
    a["ref"] = a["ref"] or "SBEE26" + "".join(random.choices(string.digits, k=8))
    LEDGER.append({"app_id": a["id"], "txn": t["txn"], "amount": t["amount"], "ts": t["ts"]})
    return t


@app.route("/")
def landing():
    extra = """
<div id="cookie">We use cookies to improve your experience. <button class="btn" onclick="this.parentNode.remove()">Accept All</button></div>
<div id="ad"><div><span class="x" onclick="document.getElementById('ad').remove()">close [x]</span>
<h3>Download the SBEE Mobile App!</h3><p>Get alerts &amp; results on your phone</p>
<button class="btn" onclick="alert('App store unavailable')">Install Now</button></div></div>"""
    body = """
<h3>Notices</h3><ul><li>Admit card release: 12-DEC-2026</li><li>Syllabus revised <span class="tiny">(click here for PDF)</span></li>
<li>Fee refund policy updated</li></ul>
<table width="100%"><tr><td><b>Existing candidates</b><br><a href="/login">Login to dashboard</a><br><a href="/forgot">Forgot password?</a></td>
<td><b>Other services</b><br><a href="/forgot">Download admit card</a><br><a href="/forgot">Check result</a><br>
<span class="tiny">New candidate? <a href="/register">Click here to register</a></span></td></tr></table>"""
    return page(body, extra)


@app.route("/login")
@app.route("/forgot")
def dead_end():
    return page('<p>Service temporarily unavailable. Please try again later.</p><a href="/">Back to home</a>')


@app.route("/register", methods=["GET", "POST"])
def register():
    err = ""
    form = {}
    if request.method == "POST":
        form = request.form.to_dict()
        d, m, y = form.get("dd"), form.get("mm"), form.get("yyyy")
        if not form.get("name", "").strip():
            err = "Candidate name is mandatory"
        elif not (d and m and y and d != "DD" and m != "MM" and y != "YYYY"):
            err = "Please select a valid Date of Birth"
        elif not (form.get("mobile", "").isdigit() and len(form["mobile"]) == 10):
            err = "Mobile number must be exactly 10 digits (no +91 / spaces)"
        elif not (form.get("aadhaar", "").replace(" ", "").isdigit() and len(form["aadhaar"].replace(" ", "")) == 12):
            err = "Aadhaar must be 12 digits"
        elif not form.get("category") or form["category"].startswith("--"):
            err = "Select a category"
        elif not form.get("email") or "@" not in form["email"]:
            err = "Enter a valid email"
        if not err:
            app_id = "APP" + "".join(random.choices(string.digits, k=6))
            APPS[app_id] = {"id": app_id, "profile": form, "epoch": 0, "paid": False, "ref": None, "txns": [], "upi": None}
            session["app_id"], session["epoch"] = app_id, 0
            session["profile"] = form
            otp = "".join(random.choices(string.digits, k=6))
            session["otp"] = otp
            SMS_INBOX.append({"to": form["mobile"], "from": "SBEEPT", "text": f"{otp} is your OTP for SBEE-2026 registration. Do not share.", "ts": time.time()})
            return redirect("/otp")
    opts = lambda a: "".join(f"<option>{x}</option>" for x in a)
    body = steps(0) + """
{% if err %}<div class="err">{{ err }}</div>{% endif %}
<form method="post"><table class="f">
<tr><td class="l">Candidate's Name <span class="req">*</span><br><span class="tiny">(as per 10th certificate)</span></td><td><input type="text" name="name" value="{{f.get('name','')}}"></td></tr>
<tr><td class="l">Date of Birth <span class="req">*</span></td><td>
<select name="dd" style="min-width:60px"><option>DD</option>{{ days|safe }}</select>
<select name="mm" style="min-width:60px"><option>MM</option>{{ months|safe }}</select>
<select name="yyyy" style="min-width:80px"><option>YYYY</option>{{ years|safe }}</select></td></tr>
<tr><td class="l">Mobile No. <span class="req">*</span></td><td><input type="text" name="mobile" maxlength="10" value="{{f.get('mobile','')}}"></td></tr>
<tr><td class="l">E-mail ID <span class="req">*</span></td><td><input type="text" name="email" value="{{f.get('email','')}}"></td></tr>
<tr><td class="l">Aadhaar No. <span class="req">*</span></td><td><input type="text" name="aadhaar" value="{{f.get('aadhaar','')}}"></td></tr>
<tr><td class="l">Category <span class="req">*</span></td><td><select name="category"><option>-- Select --</option>
<option>GEN</option><option>GM (General Merit)</option><option>OBC-NCL</option><option>SC</option><option>ST</option><option>EWS</option></select></td></tr>
<tr><td class="l">Father's Name</td><td><input type="text" name="father" value="{{f.get('father','')}}"></td></tr>
<tr><td></td><td><button class="btn" type="submit">Save &amp; Continue</button></td></tr></table></form>"""
    return page(
        body,
        err=err,
        f=form,
        days=opts(f"{i:02d}" for i in range(1, 32)),
        months=opts(f"{i:02d}" for i in range(1, 13)),
        years=opts(range(2010, 1989, -1)),
    )


@app.route("/otp", methods=["GET", "POST"])
def otp():
    if "otp" not in session:
        return redirect("/register")
    err = ""
    if request.method == "POST":
        if request.form.get("otp") == session["otp"]:
            session["otp_ok"] = True
            return redirect("/centre")
        err = "Invalid OTP. Please try again."
    body = steps(1) + """{% if err %}<div class="err">{{err}}</div>{% endif %}
<p>An OTP has been sent to mobile ending <b>{{ m }}</b>.</p>
<form method="post"><input type="text" name="otp" maxlength="6" placeholder="Enter OTP"> <button class="btn" type="submit">Verify</button></form>"""
    return page(body, err=err, m=session["profile"]["mobile"][-4:])


@app.route("/centre", methods=["GET", "POST"])
def centre():
    if not session.get("otp_ok"):
        return redirect("/register")
    if request.method == "POST":
        if request.form.get("centre") and request.form.get("city"):
            session["centre"] = request.form["centre"]
            session["city"] = request.form["city"]
            session["state"] = request.form.get("state")
            if _app():
                _app().update(centre=session["centre"], city=session["city"], state=session["state"])
            return redirect("/captcha")
    body = steps(2) + """<form method="post"><table class="f">
<tr><td class="l">State <span class="req">*</span></td><td><select name="state" id="st" onchange="fillCity()"><option>-- Select --</option>{% for s in cities %}<option>{{s}}</option>{% endfor %}</select></td></tr>
<tr><td class="l">City <span class="req">*</span></td><td><select name="city" id="ct" onchange="fillCentre()"><option>-- Select --</option></select></td></tr>
<tr><td class="l">Preferred Centre <span class="req">*</span></td><td id="cl"><span class="tiny">Select city first</span></td></tr>
<tr><td></td><td><button class="btn" type="submit">Continue</button></td></tr></table></form>
<script>
var C={{ cities|tojson }},Z={{ centres|tojson }};
function fillCity(){var s=document.getElementById('st').value,c=document.getElementById('ct');c.innerHTML='<option>-- Select --</option>';(C[s]||[]).forEach(function(x){c.innerHTML+='<option>'+x+'</option>'});document.getElementById('cl').innerHTML='<span class="tiny">Select city first</span>'}
function fillCentre(){var c=document.getElementById('ct').value,h='';(Z[c]||[]).forEach(function(x,i){h+='<label><input type="radio" name="centre" value="'+x+'"> '+x+'</label><br>'});document.getElementById('cl').innerHTML=h||'-'}
</script>"""
    return page(body, cities=CITIES, centres=CENTRES)


@app.route("/captcha.svg")
def captcha_svg():
    code = session.get("captcha") or ("K7M2Q" if os.getenv("FORMPILOT_BENCH") == "1" else "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=5)))
    session["captcha"] = code
    rnd = random.Random()
    noise = "".join(
        f'<line x1="{rnd.randint(0,170)}" y1="{rnd.randint(0,50)}" x2="{rnd.randint(0,170)}" y2="{rnd.randint(0,50)}" stroke="#{rnd.randint(0x555,0xbbb):x}" stroke-width="1.5"/>'
        for _ in range(7)
    )
    chars = "".join(
        f'<text x="{14+i*30}" y="{rnd.randint(30,40)}" font-size="{rnd.randint(26,32)}" font-family="Courier" font-weight="bold" '
        f'transform="rotate({rnd.randint(-22,22)} {20+i*30} 28)" fill="#{rnd.randint(0x222,0x66a):x}">{c}</text>'
        for i, c in enumerate(code)
    )
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="170" height="50" style="background:#eef">{noise}{chars}{noise}</svg>'
    return svg, 200, {"Content-Type": "image/svg+xml", "Cache-Control": "no-store"}


@app.route("/captcha", methods=["GET", "POST"])
def captcha():
    if "centre" not in session:
        return redirect("/register")
    err = ""
    if request.method == "POST":
        if request.form.get("cap", "").strip().upper() == session.get("captcha"):
            session["cap_ok"] = True
            session.pop("captcha", None)
            if _app():
                _app()["cap_ok"] = True
            return redirect("/review")
        err = "Incorrect security code"
        session.pop("captcha", None)
    body = steps(3) + """{% if err %}<div class="err">{{err}}</div>{% endif %}
<p>Enter the characters shown in the image (case-insensitive):</p>
<img src="/captcha.svg?t={{t}}" width="170" height="50"><br><br>
<form method="post"><input type="text" name="cap" maxlength="5"> <button class="btn" type="submit">Submit</button></form>"""
    return page(body, err=err, t=time.time())


@app.route("/review", methods=["GET", "POST"])
def review():
    if not session.get("cap_ok"):
        return redirect("/register")
    err = ""
    if request.method == "POST":
        if request.form.get("decl"):
            return redirect("/pay")
        err = "You must accept the declaration"
    p = session["profile"]
    body = steps(4) + """{% if err %}<div class="err">{{err}}</div>{% endif %}
<table class="f" border="1" style="border-collapse:collapse">
{% for k,v in rows %}<tr><td class="l">{{k}}</td><td>{{v}}</td></tr>{% endfor %}</table><br>
<form method="post"><label><input type="checkbox" name="decl" value="1"> I hereby declare that all information furnished is true and correct to the best of my knowledge.</label><br><br>
<button class="btn2" type="button" onclick="history.back()">Edit</button> <button class="btn" type="submit">Proceed to Payment</button></form>"""
    rows = [("Name", p["name"]), ("DOB", f"{p['dd']}/{p['mm']}/{p['yyyy']}"), ("Mobile", p["mobile"]), ("Email", p["email"]),
            ("Category", p["category"]), ("Centre", f"{session['centre']}, {session['city']}")]
    return page(body, err=err, rows=rows)


FEES = {"GEN": 800, "GM (General Merit)": 800, "OBC-NCL": 800, "EWS": 800, "SC": 400, "ST": 400}


@app.route("/pay", methods=["GET", "POST"])
def pay():
    a = _app()
    if not session.get("cap_ok") or not a:
        return redirect("/register")
    fee = FEES.get(session["profile"]["category"], 800)
    err = ""
    if request.method == "POST":
        if request.form.get("mode") == "upi" and "@" in request.form.get("upi", ""):
            _charge(a, fee, request.form["upi"])  # money moves here, whatever happens next
            if FAULT["mode"] == "expire_after_pay":
                FAULT["mode"] = ""
                a["txns"][-1]["status"] = "pending"
                a["epoch"] += 1
                session.clear()
                return redirect("/expired")
            return redirect("/done")
        err = "Select UPI and enter a valid UPI ID (name@bank)"
    body = steps(5) + """{% if err %}<div class="err">{{err}}</div>{% endif %}
{% if earlier %}<p class="tiny" style="background:#ffd;border:1px solid #cc0;padding:4px">A previous payment attempt exists for this application.
<a href="/pay-status">Check payment status</a></p>{% endif %}
<p>Application fee payable: <b>Rs. {{fee}}/-</b> (+ Rs. 18 convenience charge)</p>
<form method="post"><label><input type="radio" name="mode" value="card"> Credit/Debit card</label><br>
<label><input type="radio" name="mode" value="nb"> Net banking</label><br>
<label><input type="radio" name="mode" value="upi"> UPI</label> &nbsp; <input type="text" name="upi" placeholder="name@bank"><br><br>
<button class="btn" type="submit">Pay Now</button></form>"""
    return page(body, err=err, fee=fee, earlier=bool(a["txns"]))


@app.route("/done")
def done():
    a = _app()
    if not a or not a["paid"]:
        return redirect("/register")
    body = steps(6) + """<h3 style="color:green">Registration Successful</h3>
<p>Application Reference No: <b id="ref">{{ref}}</b></p><p>Candidate: {{n}}<br>Centre: {{c}}</p>
<p class="tiny">Please save this reference number. Admit card will be available from 12-DEC-2026.</p>"""
    return page(body, ref=a["ref"], n=a["profile"]["name"], c=f"{a.get('centre')}, {a.get('city')}")


@app.route("/expired")
def expired():
    return page("""<h3 style="color:#b00">Session Expired</h3>
<p>Your session has timed out for security reasons. Any transaction in progress may or may not have been completed.</p>
<p><a href="/resume">Login again</a> to continue your application.</p>""")


@app.route("/resume", methods=["GET", "POST"])
def resume():
    err = ""
    if request.method == "POST":
        mob = request.form.get("mobile", "").strip()
        dob = request.form.get("dob", "").strip()
        a = next((x for x in reversed(list(APPS.values()))
                  if x["profile"].get("mobile") == mob and f"{x['profile'].get('dd')}/{x['profile'].get('mm')}/{x['profile'].get('yyyy')}" == dob), None)
        if a:
            session.clear()
            session.update(app_id=a["id"], epoch=a["epoch"], profile=a["profile"], otp_ok=True)
            if a.get("centre"):
                session.update(centre=a["centre"], city=a["city"], state=a.get("state"))
            if a.get("cap_ok"):
                session["cap_ok"] = True
            if a["txns"]:
                return redirect("/pay-status")
            return redirect("/review" if a.get("cap_ok") else "/captcha" if a.get("centre") else "/centre")
        err = "No application found for this mobile number and date of birth"
    body = """<h3>Candidate Login</h3>{% if err %}<div class="err">{{err}}</div>{% endif %}
<form method="post"><table class="f">
<tr><td class="l">Registered Mobile No.</td><td><input type="text" name="mobile" maxlength="10"></td></tr>
<tr><td class="l">Date of Birth <span class="tiny">(DD/MM/YYYY)</span></td><td><input type="text" name="dob" maxlength="10" placeholder="DD/MM/YYYY"></td></tr>
<tr><td></td><td><button class="btn" type="submit">Login</button></td></tr></table></form>"""
    return page(body, err=err)


@app.route("/pay-status", methods=["GET", "POST"])
def pay_status():
    a = _app()
    if not a:
        return redirect("/register")
    if request.method == "POST":
        if request.form.get("act") == "refresh":
            for t in a["txns"]:
                if t["status"] == "pending":
                    t["status"] = "success"
        elif request.form.get("act") == "pay" and a.get("upi"):
            _charge(a, FEES.get(a["profile"]["category"], 800), a["upi"])  # naive portal: charges again
        return redirect("/pay-status")
    pending = any(t["status"] == "pending" for t in a["txns"])
    body = steps(5) + """<h3>Payment Status</h3>
<table class="f" border="1" style="border-collapse:collapse"><tr><td class="l">Transaction</td><td><b>Amount</b></td><td><b>Status</b></td></tr>
{% for t in txns %}<tr><td>{{t.txn}}</td><td>Rs. {{t.amount}}</td><td>{{ 'Awaiting bank confirmation' if t.status == 'pending' else 'SUCCESS' }}</td></tr>{% endfor %}</table><br>
{% if pending %}<p>The status of your last payment could not be confirmed. If the amount was debited it will reflect once the bank confirms.</p>
<form method="post" style="display:inline"><button class="btn2" name="act" value="refresh">Refresh Status</button></form>
<form method="post" style="display:inline"><button class="btn" name="act" value="pay">Pay Again</button></form>
{% elif paid %}<p style="color:green"><b>Payment received.</b> Your application fee has been paid.</p><a class="btn" style="text-decoration:none" href="/done">View Receipt</a>
{% else %}<a href="/pay">Go to payment</a>{% endif %}"""
    return page(body, txns=a["txns"], pending=pending, paid=a["paid"])


if __name__ == "__main__":
    app.run(port=5050, debug=False)
