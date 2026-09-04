/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026. Sample integration provided under the MIT License
 * (see LICENSE). Provided "as is", without warranty of any kind. */
/*
 * tts-widget.js — drop-in Polly text-to-speech widget.
 *
 * Usage in the host app's page:
 *   <script src="/static/tts-widget.js"></script>
 *   <script>
 *     PollyTTS.init({
 *       mount: "#tts",              // container element (selector or node)
 *       baseUrl: "/tts",            // must match the blueprint's url_prefix
 *     });
 *   </script>
 *
 * The widget renders a textarea, a language/voice <select> (with an
 * "Auto-detect language" option), and a Speak button, and plays the returned
 * MP3 through an <audio> element. No framework or build step required.
 */
(function (global) {
  "use strict";

  async function init(opts) {
    const baseUrl = (opts.baseUrl || "/tts").replace(/\/$/, "");
    const mount =
      typeof opts.mount === "string"
        ? document.querySelector(opts.mount)
        : opts.mount;
    if (!mount) throw new Error("PollyTTS: mount element not found");

    mount.innerHTML = `
      <div class="tts-widget">
        <textarea class="tts-text" rows="5" placeholder="Type something to hear it spoken…"></textarea>
        <div class="tts-controls">
          <select class="tts-voice" aria-label="Language and voice">
            <option value="auto" data-engine="">🌐 Auto-detect language</option>
          </select>
          <button type="button" class="tts-speak">Speak</button>
          <span class="tts-spinner" hidden>…</span>
        </div>
        <p class="tts-status" role="status"></p>
        <p class="tts-error" role="alert" style="color:#c0392b"></p>
        <audio class="tts-player" controls hidden></audio>
      </div>`;

    const textEl = mount.querySelector(".tts-text");
    const voiceEl = mount.querySelector(".tts-voice");
    const btn = mount.querySelector(".tts-speak");
    const spinner = mount.querySelector(".tts-spinner");
    const statusEl = mount.querySelector(".tts-status");
    const errorEl = mount.querySelector(".tts-error");
    const player = mount.querySelector(".tts-player");

    // Populate the voice list, grouped by language.
    try {
      const res = await fetch(`${baseUrl}/voices`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const voices = await res.json();
      const groups = new Map();
      for (const v of voices) {
        if (!groups.has(v.lang)) groups.set(v.lang, []);
        groups.get(v.lang).push(v);
      }
      for (const [lang, list] of groups) {
        const og = document.createElement("optgroup");
        og.label = lang;
        for (const v of list) {
          const opt = document.createElement("option");
          opt.value = v.id;
          opt.dataset.engine = v.engine;
          opt.textContent = `${v.id} (${v.gender})`;
          og.appendChild(opt);
        }
        voiceEl.appendChild(og);
      }
    } catch (e) {
      errorEl.textContent = `Could not load voices: ${e.message}`;
    }

    btn.addEventListener("click", async () => {
      const text = textEl.value.trim();
      errorEl.textContent = "";
      statusEl.textContent = "";
      if (!text) {
        errorEl.textContent = "Please enter some text.";
        return;
      }

      btn.disabled = true;
      spinner.hidden = false;
      try {
        const selected = voiceEl.options[voiceEl.selectedIndex];
        const isAuto = voiceEl.value === "auto";
        const engine = !isAuto && selected ? selected.dataset.engine : undefined;

        const res = await fetch(`${baseUrl}/synthesize`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text, voice: voiceEl.value, engine }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({ error: "Synthesis failed" }));
          throw new Error(err.error || `HTTP ${res.status}`);
        }

        const detected = res.headers.get("X-Detected-Language");
        const voiceUsed = res.headers.get("X-Voice-Used");
        if (isAuto && detected) {
          statusEl.textContent = `Detected ${detected} → voice ${voiceUsed}`;
        } else if (voiceUsed) {
          statusEl.textContent = `Voice: ${voiceUsed}`;
        }

        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        if (player.src && player.src.startsWith("blob:")) {
          URL.revokeObjectURL(player.src);
        }
        player.src = url;
        player.hidden = false;
        player.play();
      } catch (e) {
        errorEl.textContent = e.message;
      } finally {
        btn.disabled = false;
        spinner.hidden = true;
      }
    });
  }

  global.PollyTTS = { init };
})(window);
