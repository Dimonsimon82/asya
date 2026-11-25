"use strict";

(function () {
  var $ = function (id) { return document.getElementById(id); };

  var S = {
    token: null, uin: null, nick: "", status: "online",
    contacts: [], current: null, since: 0, v: -1,
    sound: true, polling: false, shown: {}, authCtx: null
  };

  var STATUS_RU = { online: "в сети", away: "отошёл", dnd: "не беспокоить", offline: "не в сети" };

  // ---------- хранилище (может быть недоступно) ----------
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } }
  function lsDel(k) { try { localStorage.removeItem(k); } catch (e) { /* ignore */ } }

  // ---------- HTTP ----------
  function api(method, path, body, raw) {
    var opt = { method: method, headers: {} };
    if (S.token) opt.headers["Authorization"] = "Bearer " + S.token;
    if (raw !== undefined) {
      opt.body = raw;
      opt.headers["Content-Type"] = "application/octet-stream";
    } else if (body !== undefined) {
      opt.body = JSON.stringify(body);
      opt.headers["Content-Type"] = "application/json";
    }
    return fetch(path, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (r.status === 401 && S.token) { doLogout(true); }
        if (!r.ok) {
          var e = new Error(d.error || ("Ошибка " + r.status));
          e.status = r.status;
          throw e;
        }
        return d;
      });
    });
  }

  function flash(el, text, ok) {
    el.textContent = text || "";
    el.classList.toggle("ok", !!ok);
    if (text) {
      var t = text;
      setTimeout(function () { if (el.textContent === t) el.textContent = ""; }, 5000);
    }
  }

  // ---------- звук (как «uh-oh», только синтез) ----------
  var actx = null;
  function beep() {
    if (!S.sound) return;
    try {
      actx = actx || new (window.AudioContext || window.webkitAudioContext)();
      if (actx.state === "suspended") actx.resume();
      var t0 = actx.currentTime;
      [[740, 0], [554, 0.14]].forEach(function (p) {
        var o = actx.createOscillator();
        var g = actx.createGain();
        o.type = "square";
        o.frequency.value = p[0];
        g.gain.setValueAtTime(0.0001, t0 + p[1]);
        g.gain.exponentialRampToValueAtTime(0.12, t0 + p[1] + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + p[1] + 0.16);
        o.connect(g); g.connect(actx.destination);
        o.start(t0 + p[1]); o.stop(t0 + p[1] + 0.18);
      });
    } catch (e) { /* звук недоступен */ }
  }

  // ---------- вход / выход ----------
  function showAuth() {
    $("app").hidden = true;
    $("auth").hidden = false;
  }

  function enterApp() {
    $("auth").hidden = true;
    $("app").hidden = false;
    $("me-nick").textContent = S.nick;
    $("me-uin").textContent = S.uin;
    api("GET", "/api/me").then(function (d) {
      S.status = d.status;
      S.since = d.last;
      $("status").value = d.status;
      S.contacts = d.contacts;
      renderContacts();
      S.polling = true;
      poll();
    }).catch(function () { /* 401 обработан в api() */ });
  }

  function saveSession(d) {
    S.token = d.token; S.uin = d.uin; S.nick = d.nick;
    lsSet("asya_token", d.token);
    lsSet("asya_uin", String(d.uin));
    lsSet("asya_nick", d.nick);
  }

  function doLogout(silent) {
    var tok = S.token;
    S.polling = false;
    S.token = null; S.current = null; S.contacts = []; S.since = 0; S.v = -1; S.shown = {};
    lsDel("asya_token"); lsDel("asya_uin"); lsDel("asya_nick");
    if (tok && !silent) {
      fetch("/api/logout", { method: "POST", headers: { "Authorization": "Bearer " + tok } }).catch(function () {});
    }
    $("log").textContent = "";
    $("form-send").hidden = true;
    $("chat-title").textContent = "Выберите контакт слева";
    $("app").classList.remove("in-chat");
    showAuth();
    if (silent) flash($("auth-msg"), "Сессия закончилась, войдите снова");
  }

  $("tab-login").addEventListener("click", function () {
    $("tab-login").classList.add("active"); $("tab-reg").classList.remove("active");
    $("form-login").hidden = false; $("form-reg").hidden = true;
  });
  $("tab-reg").addEventListener("click", function () {
    $("tab-reg").classList.add("active"); $("tab-login").classList.remove("active");
    $("form-reg").hidden = false; $("form-login").hidden = true;
  });

  $("form-login").addEventListener("submit", function (e) {
    e.preventDefault();
    api("POST", "/api/login", { uin: $("l-uin").value, password: $("l-pw").value })
      .then(function (d) { $("l-pw").value = ""; saveSession(d); enterApp(); })
      .catch(function (err) { flash($("auth-msg"), err.message); });
  });

  $("form-reg").addEventListener("submit", function (e) {
    e.preventDefault();
    api("POST", "/api/register", { nick: $("r-nick").value, password: $("r-pw").value })
      .then(function (d) {
        $("r-pw").value = "";
        saveSession(d);
        enterApp();
        flash($("side-msg"), "Ваш UIN: " + d.uin + " — запишите его!", true);
      })
      .catch(function (err) { flash($("auth-msg"), err.message); });
  });

  $("btn-logout").addEventListener("click", function () { doLogout(false); });

  $("btn-sound").addEventListener("click", function () {
    S.sound = !S.sound;
    $("btn-sound").textContent = "Звук: " + (S.sound ? "вкл" : "выкл");
    lsSet("asya_sound", S.sound ? "1" : "0");
    if (S.sound) beep();
  });

  $("status").addEventListener("change", function () {
    api("POST", "/api/status", { status: $("status").value }).catch(function (err) {
      flash($("side-msg"), err.message);
    });
  });

  // ---------- контакты ----------
  function renderContacts() {
    var box = $("contacts");
    box.textContent = "";
    if (!S.contacts.length) {
      var em = document.createElement("div");
      em.className = "empty";
      em.textContent = "Контактов нет. Добавьте UIN ниже.";
      box.appendChild(em);
      return;
    }
    var order = { online: 0, away: 1, dnd: 2, offline: 3 };
    var list = S.contacts.slice().sort(function (a, b) {
      return (order[a.status] - order[b.status]) || a.nick.localeCompare(b.nick);
    });
    list.forEach(function (c) {
      var row = document.createElement("div");
      row.className = "contact" + (S.current === c.uin ? " sel" : "");
      row.title = STATUS_RU[c.status] || c.status;

      var dot = document.createElement("span");
      dot.className = "dot " + c.status;
      row.appendChild(dot);

      var nm = document.createElement("span");
      nm.className = "nm";
      nm.textContent = c.nick;
      var un = document.createElement("span");
      un.className = "un";
      un.textContent = " " + c.uin;
      nm.appendChild(un);
      row.appendChild(nm);

      if (c.unread > 0 && S.current !== c.uin) {
        var b = document.createElement("span");
        b.className = "badge";
        b.textContent = c.unread;
        row.appendChild(b);
      }

      var rm = document.createElement("button");
      rm.className = "rm";
      rm.type = "button";
      rm.title = "Убрать из списка";
      rm.textContent = "✕";
      rm.addEventListener("click", function (ev) {
        ev.stopPropagation();
        if (!confirm("Убрать " + c.nick + " из списка контактов?")) return;
        api("POST", "/api/contacts/remove", { uin: c.uin }).then(function () {
          if (S.current === c.uin) closeChat();
        }).catch(function (err) { flash($("side-msg"), err.message); });
      });
      row.appendChild(rm);

      row.addEventListener("click", function () { openChat(c.uin); });
      box.appendChild(row);
    });
  }

  $("form-add").addEventListener("submit", function (e) {
    e.preventDefault();
    var v = $("add-uin").value;
    api("POST", "/api/contacts/add", { uin: v }).then(function () {
      $("add-uin").value = "";
      flash($("side-msg"), "Контакт добавлен", true);
    }).catch(function (err) { flash($("side-msg"), err.message); });
  });

  // ---------- чат ----------
  function contactByUin(u) {
    for (var i = 0; i < S.contacts.length; i++) if (S.contacts[i].uin === u) return S.contacts[i];
    return null;
  }

  function fmtTime(ts) {
    var d = new Date(ts * 1000);
    var hm = ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
    var today = new Date();
    if (d.toDateString() === today.toDateString()) return hm;
    return ("0" + d.getDate()).slice(-2) + "." + ("0" + (d.getMonth() + 1)).slice(-2) + " " + hm;
  }

  function fmtSize(n) {
    if (n < 1024) return n + " Б";
    if (n < 1048576) return (n / 1024).toFixed(1) + " КБ";
    return (n / 1048576).toFixed(1) + " МБ";
  }

  function addBubble(m) {
    if (S.shown[m.id]) return;
    S.shown[m.id] = true;
    var log = $("log");
    var empty = log.querySelector(".empty");
    if (empty) empty.remove();

    var mine = m.from === S.uin;
    var box = document.createElement("div");
    box.className = "m" + (mine ? " me" : "");

    var meta = document.createElement("div");
    meta.className = "meta";
    var who = mine ? "Вы" : (contactByUin(m.from) ? contactByUin(m.from).nick : String(m.from));
    meta.textContent = who + " · " + fmtTime(m.ts);
    box.appendChild(meta);

    if (m.kind === "file") {
      var a = document.createElement("a");
      a.href = "#";
      a.textContent = "📎 " + m.file.name + " (" + fmtSize(m.file.size) + ")";
      a.addEventListener("click", function (ev) {
        ev.preventDefault();
        download(m);
      });
      box.appendChild(a);
    } else {
      var t = document.createElement("div");
      t.className = "txt";
      t.textContent = m.text;
      box.appendChild(t);
    }
    log.appendChild(box);
    log.scrollTop = log.scrollHeight;
  }

  function download(m) {
    fetch("/api/file/" + m.id, { headers: { "Authorization": "Bearer " + S.token } })
      .then(function (r) {
        if (!r.ok) throw new Error("Файл недоступен");
        return r.blob();
      })
      .then(function (blob) {
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = m.file.name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
      })
      .catch(function (err) { flash($("chat-msg"), err.message); });
  }

  function openChat(uin) {
    S.current = uin;
    S.shown = {};
    var c = contactByUin(uin);
    $("chat-title").textContent = c ? (c.nick + " · " + uin) : String(uin);
    $("form-send").hidden = false;
    $("app").classList.add("in-chat");
    var log = $("log");
    log.textContent = "";
    renderContacts();
    api("GET", "/api/history?with=" + uin).then(function (d) {
      if (S.current !== uin) return;
      if (!d.messages.length) {
        var em = document.createElement("div");
        em.className = "empty";
        em.textContent = "Сообщений пока нет";
        log.appendChild(em);
      }
      d.messages.forEach(addBubble);
      return api("POST", "/api/read", { with: uin });
    }).catch(function (err) { flash($("chat-msg"), err.message); });
    $("text").focus();
  }

  function closeChat() {
    S.current = null;
    $("form-send").hidden = true;
    $("log").textContent = "";
    $("chat-title").textContent = "Выберите контакт слева";
    $("app").classList.remove("in-chat");
    renderContacts();
  }

  $("btn-back").addEventListener("click", closeChat);

  function sendText() {
    var ta = $("text");
    var text = ta.value;
    if (!text.trim() || S.current == null) return;
    ta.value = "";
    api("POST", "/api/send", { to: S.current, text: text }).then(function (m) {
      addBubble(m);
    }).catch(function (err) {
      ta.value = text;
      flash($("chat-msg"), err.message);
    });
  }

  $("form-send").addEventListener("submit", function (e) { e.preventDefault(); sendText(); });
  $("text").addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendText();
    }
  });

  $("file").addEventListener("change", function () {
    var f = $("file").files[0];
    $("file").value = "";
    if (!f || S.current == null) return;
    if (f.size > 25 * 1024 * 1024) { flash($("chat-msg"), "Файл больше 25 МБ"); return; }
    if (f.size === 0) { flash($("chat-msg"), "Файл пустой"); return; }
    flash($("chat-msg"), "Отправка файла…", true);
    var to = S.current;
    api("POST", "/api/upload?to=" + to + "&name=" + encodeURIComponent(f.name), undefined, f)
      .then(function (m) {
        flash($("chat-msg"), "Файл отправлен", true);
        if (S.current === to) addBubble(m);
      })
      .catch(function (err) { flash($("chat-msg"), err.message); });
  });

  // ---------- long-poll ----------
  function poll() {
    if (!S.polling) return;
    var tok = S.token;
    api("GET", "/api/poll?since=" + S.since + "&v=" + S.v).then(function (d) {
      if (!S.polling || S.token !== tok) return;
      S.v = d.v;
      S.contacts = d.contacts;
      if (d.status && $("status").value !== d.status) $("status").value = d.status;

      var needBeep = false;
      d.messages.forEach(function (m) {
        if (m.id > S.since) S.since = m.id;
        var incoming = m.to === S.uin;
        if (S.current !== null && (m.from === S.current || m.to === S.current)) {
          addBubble(m);
          if (incoming) {
            needBeep = true;
            api("POST", "/api/read", { with: S.current }).catch(function () {});
          }
        } else if (incoming) {
          needBeep = true;
        }
      });
      if (needBeep) {
        beep();
        if (document.hidden) document.title = "● Ася";
      }
      renderContacts();
      setTimeout(poll, 50);
    }).catch(function () {
      if (S.polling) setTimeout(poll, 3000);
    });
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) document.title = "Ася";
  });

  // ---------- старт ----------
  S.sound = lsGet("asya_sound") !== "0";
  $("btn-sound").textContent = "Звук: " + (S.sound ? "вкл" : "выкл");

  var savedTok = lsGet("asya_token");
  if (savedTok) {
    S.token = savedTok;
    S.uin = parseInt(lsGet("asya_uin"), 10);
    S.nick = lsGet("asya_nick") || "";
    enterApp();
  } else {
    showAuth();
  }
})();
