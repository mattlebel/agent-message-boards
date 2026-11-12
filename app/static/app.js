// Thumbs up/down on agent posts: record right away, then offer an optional comment.
(function () {
  const dialog = document.getElementById("fb-dialog");
  let currentFeedbackId = null;
  let currentPost = null;

  async function post(url, data, method = "POST") {
    const resp = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: data ? JSON.stringify(data) : undefined,
      credentials: "same-origin",
    });
    if (!resp.ok) throw new Error("request failed: " + resp.status);
    return resp.json();
  }

  function paint(container, value) {
    const up = container.querySelector("[data-vote='1']");
    const down = container.querySelector("[data-vote='-1']");
    up.classList.toggle("on-up", value === 1);
    down.classList.toggle("on-down", value === -1);
  }

  document.querySelectorAll(".votes").forEach((box) => {
    box.querySelectorAll("button[data-vote]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const value = parseInt(btn.dataset.vote, 10);
        const postId = parseInt(box.dataset.post, 10);
        const already = box.dataset.value && parseInt(box.dataset.value, 10) === value;
        try {
          if (already && box.dataset.fid) {
            await post(`/api/feedback/${box.dataset.fid}`, null, "DELETE");
            box.dataset.value = "";
            box.dataset.fid = "";
            paint(box, 0);
            return;
          }
          const res = await post("/api/feedback", { post_id: postId, value });
          box.dataset.value = String(res.value);
          box.dataset.fid = String(res.id);
          paint(box, res.value);
          if (dialog) {
            currentFeedbackId = res.id;
            currentPost = box;
            dialog.querySelector("#fb-title").textContent =
              value === 1 ? "👍 Noted. What did you like?" : "👎 Noted. What missed?";
            dialog.querySelector("textarea").value = res.comment || "";
            dialog.showModal();
            dialog.querySelector("textarea").focus();
          }
        } catch (e) {
          alert("Couldn't save that: " + e.message);
        }
      });
    });
  });

  if (dialog) {
    dialog.querySelector("form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const comment = dialog.querySelector("textarea").value.trim();
      if (comment && currentFeedbackId) {
        await post(`/api/feedback/${currentFeedbackId}/comment`, { comment });
        const note = currentPost.querySelector(".fb-comment");
        if (note) note.textContent = "“" + comment + "”";
      }
      dialog.close();
    });
    dialog.querySelector("[data-skip]").addEventListener("click", () => dialog.close());
  }

  // After you post, poll until the agents have replied, then reload.
  const waiting = document.querySelector("[data-wait-thread]");
  if (waiting) {
    const id = waiting.dataset.waitThread;
    const start = parseInt(waiting.dataset.count, 10);
    let tries = 0;
    const timer = setInterval(async () => {
      tries += 1;
      try {
        const res = await fetch(`/t/${id}/count`, { credentials: "same-origin" }).then((r) => r.json());
        if (res.count > start) {
          clearInterval(timer);
          window.location = `/t/${id}#bottom`;
        }
      } catch (e) {}
      if (tries > 40) {
        clearInterval(timer);
        waiting.textContent = "Still waiting. Refresh in a bit.";
      }
    }, 4000);
  }

  // Source form: show the config fields for the chosen kind.
  const kindSelect = document.getElementById("source-kind");
  if (kindSelect) {
    const kinds = JSON.parse(document.getElementById("kinds-json").textContent);
    const box = document.getElementById("source-fields");
    function draw() {
      const spec = kinds[kindSelect.value];
      box.innerHTML = "";
      Object.entries(spec.fields).forEach(([key, label]) => {
        const l = document.createElement("label");
        l.textContent = label;
        const input = document.createElement("input");
        input.name = "cfg_" + key;
        l.appendChild(input);
        box.appendChild(l);
      });
    }
    kindSelect.addEventListener("change", draw);
    draw();
  }
})();

// Reading page: show the note box only when "Add post for agent discussion?" is checked.
(function () {
  const toggle = document.getElementById("discuss-toggle");
  const box = document.getElementById("discuss-box");
  if (!toggle || !box) return;
  const sync = () => { box.hidden = !toggle.checked; };
  toggle.addEventListener("change", sync);
  sync();
})();
