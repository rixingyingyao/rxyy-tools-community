/* Shared native-question cards for the desktop console and the mobile view. */
(function (root) {
  "use strict";
  const drafts = new Map(), submitting = new Set(), notices = new Map(), delivered = new Set();
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }
  function render(container, turn, config) {
    const requests = turn && turn.native_questions || [];
    const visible = requests.filter(r => r.status === "pending").concat(requests.filter(r => r.status !== "pending").slice(-2));
    const sig = JSON.stringify([config.sid, visible, !!(turn && turn.desktop_connected)]);
    container.hidden = !visible.length;
    if (container.dataset.sig === sig) return;
    container.dataset.sig = sig;
    container.replaceChildren();
    for (const request of visible) {
      const requestId = request.delivery === "async"
        ? (request.item_id || request.call_id || request.request_id) : request.request_id;
      const key = JSON.stringify([config.sid, turn.turn_id || request.turn_id, requestId]);
      const values = drafts.get(key) || {};
      drafts.set(key, values);
      const pending = request.status === "pending";
      const connected = !!turn.desktop_connected;
      const locked = submitting.has(key) || delivered.has(key) || !!request.delivery_state;
      const card = el("section", "nq-card" + (pending ? " pending" : ""));
      const head = el("div", "nq-head");
      head.append(el("strong", "", "Codex 原生问题"), el("span", "nq-state", pending ? (locked ? "已提交 · 待同步" : "待回答") : ["answered", "resolved"].includes(request.status) ? "已回答" : "已结束"));
      card.append(head);
      for (const q of request.questions || []) {
        const row = el("div", "nq-question");
        row.append(el("div", "nq-title", q.question || q.title || q.prompt || "问题"));
        if (pending && !Object.prototype.hasOwnProperty.call(request.answers || {}, q.id)) {
          const options = el("div", "nq-options");
          const input = el("textarea", "nq-input");
          input.rows = 2; input.maxLength = 12000;
          input.placeholder = "选择上方选项，或输入你的回答";
          input.setAttribute("aria-label", q.question || q.title || "回答");
          input.value = values[q.id] || "";
          input.disabled = locked;
          input.oninput = () => { values[q.id] = input.value; };
          for (const raw of q.options || []) {
            const label = typeof raw === "string" ? raw : (raw.label || raw.id || "");
            const button = el("button", "nq-option", label);
            button.type = "button";
            button.title = typeof raw === "object" ? (raw.description || "") : "";
            button.disabled = locked;
            button.onclick = () => { values[q.id] = label; input.value = label; input.focus(); };
            options.append(button);
          }
          row.append(options, input);
        } else {
          const answer = (request.answers || {})[q.id];
          const text = Array.isArray(answer) ? answer.join("；") : typeof answer === "object" && answer ? (answer.answers || []).join("；") : answer;
          if (text) row.append(el("div", "nq-answer", text));
        }
        card.append(row);
      }
      if (pending) {
        const foot = el("div", "nq-foot");
        const note = el("span", "nq-note", notices.get(key) || (locked ? "回答已提交，请等待同步；结果未确认时请回 Codex 核对" : connected ? "回答会送回这个 Codex 任务" : "原生连接未就绪，可先填写；请连接后提交或回 Codex 回答"));
        const submit = el("button", "nq-submit", submitting.has(key) ? "提交中…" : "提交到 Codex");
        submit.type = "button"; submit.disabled = !connected || locked;
        if (locked && !submitting.has(key)) submit.textContent = "已提交";
        submit.onclick = async () => {
          const answers = {};
          const remaining = (request.questions || []).filter(q => !Object.prototype.hasOwnProperty.call(request.answers || {}, q.id));
          for (const q of remaining) if (String(values[q.id] || "").trim()) answers[q.id] = values[q.id].trim();
          if (!remaining.length || Object.keys(answers).length !== remaining.length) { note.textContent = "请完成卡片里尚未回答的问题"; return; }
          if (submitting.has(key)) return;
          submitting.add(key); submit.disabled = true; submit.textContent = "提交中…";
          let result;
          try {
            result = await config.submit({sid: config.sid, thread_id: config.threadId,
              turn_id: request.turn_id || turn.turn_id, request_id: requestId, answers});
          } catch (_) { result = {ok: false, delivery_unknown: true, error: "连接中断，结果未确认；请回 Codex 核对，不会自动重发"}; }
          submitting.delete(key);
          if (result && (result.ok || result.delivery_unknown)) delivered.add(key);
          const text = result && result.ok
            ? (result.delivery === "native_accepted" ? "Codex 已接收回答" : "已提交给原生问题，等待状态同步")
            : (result && result.error || "回答尚未送达");
          notices.set(key, text); note.textContent = text;
          submit.textContent = result && result.ok ? "已提交" : "提交到 Codex";
          submit.disabled = delivered.has(key);
          if (result && result.ok) config.refresh();
        };
        foot.append(note, submit); card.append(foot);
      }
      container.append(card);
    }
    // Only retain a small number of question drafts; native task history is authoritative.
    if (drafts.size > 80) for (const key of [...drafts.keys()].slice(0, drafts.size - 80)) { drafts.delete(key); notices.delete(key); delivered.delete(key); }
  }
  const css = ".nq-card{border:1px solid var(--border,#dfe3ed);border-radius:14px;background:var(--bg-2,#fff);padding:16px;margin:10px 0;color:var(--text,#202535)}.nq-card.pending{border-color:var(--primary,#6c64f5);box-shadow:0 3px 14px #6c64f510}.nq-head,.nq-foot{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.nq-head{font-size:13px}.nq-state{font-size:11px;color:var(--primary,#6c64f5);background:#8278fa12;border-radius:20px;padding:3px 8px}.nq-question{margin:12px 0}.nq-title{font-size:14px;line-height:1.6;white-space:pre-wrap;overflow-wrap:anywhere}.nq-options{display:flex;gap:7px;flex-wrap:wrap;margin:9px 0}.nq-option,.nq-submit{font:inherit;font-size:13px;border-radius:8px;padding:7px 11px;cursor:pointer}.nq-option{color:inherit;border:1px solid var(--border,#dde0e9);background:var(--bg,#f8f9fc);text-align:left}.nq-option:hover{border-color:var(--primary,#6c64f5)}.nq-input{box-sizing:border-box;width:100%;resize:vertical;min-height:65px;background:var(--bg,#f8f9fc);border:1px solid var(--border,#dde0e9);color:inherit;border-radius:9px;padding:10px;font:inherit;font-size:13px;line-height:1.5}.nq-input:focus{outline:2px solid #8278fa55}.nq-note{font-size:12px;color:var(--text-2,#6b7184);flex:1;min-width:170px;line-height:1.5}.nq-submit{border:0;background:var(--primary,#6c64f5);color:white}.nq-submit:disabled{opacity:.5;cursor:default}.nq-answer{font-size:13px;white-space:pre-wrap;background:#8278fa0d;padding:9px;border-radius:8px;margin-top:8px}";
  const style = el("style"); style.textContent = css; document.head.append(style);
  root.RxyyMcpNativeQuestions = {render};
  root.ChijiuNativeQuestions = root.RxyyMcpNativeQuestions; // legacy cached pages
})(window);
