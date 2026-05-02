// conclair UI client-side helpers.
// - Persists author name to localStorage and injects it into all HTMX requests.
// - Tracks recently visited projects (LRU 10) and renders them on the landing page.

(function () {
    "use strict";

    const KEY_AUTHOR = "conclair.author";
    const KEY_RECENT = "conclair.recent_projects";
    const RECENT_MAX = 10;

    // ---- author -------------------------------------------------------------

    function loadAuthor() {
        return localStorage.getItem(KEY_AUTHOR) || "";
    }

    function saveAuthor(value) {
        if (value && value.trim()) {
            localStorage.setItem(KEY_AUTHOR, value.trim());
        } else {
            localStorage.removeItem(KEY_AUTHOR);
        }
    }

    function setupAuthorInput() {
        const input = document.getElementById("author-input");
        if (!input) return;
        input.value = loadAuthor();
        input.addEventListener("change", () => saveAuthor(input.value));
        input.addEventListener("blur", () => saveAuthor(input.value));
    }

    // ---- recent projects ----------------------------------------------------

    function loadRecent() {
        try {
            const raw = localStorage.getItem(KEY_RECENT);
            if (!raw) return [];
            const arr = JSON.parse(raw);
            return Array.isArray(arr) ? arr.filter((p) => typeof p === "string") : [];
        } catch (e) {
            return [];
        }
    }

    function saveRecent(arr) {
        localStorage.setItem(KEY_RECENT, JSON.stringify(arr.slice(0, RECENT_MAX)));
    }

    function pushRecent(project) {
        if (!project) return;
        const list = loadRecent().filter((p) => p !== project);
        list.unshift(project);
        saveRecent(list);
    }

    function renderRecent() {
        const ul = document.getElementById("recent-projects");
        if (!ul) return;
        const list = loadRecent();
        if (list.length === 0) {
            ul.innerHTML = '<li class="empty-state">no recent projects</li>';
            return;
        }
        ul.innerHTML = list
            .map(
                (p) =>
                    `<li><a href="/ui/projects/${encodeURIComponent(p)}/threads">${escapeHtml(p)}</a></li>`
            )
            .join("");
    }

    function escapeHtml(s) {
        return String(s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    // ---- public: form handler used by landing.html --------------------------

    window.conclairOpenProject = function (event) {
        event.preventDefault();
        const input = document.getElementById("project-input");
        if (!input) return false;
        const project = input.value.trim();
        if (!project) return false;
        pushRecent(project);
        window.location.href = `/ui/projects/${encodeURIComponent(project)}/threads`;
        return false;
    };

    // ---- HTMX integration ---------------------------------------------------

    document.body.addEventListener("htmx:configRequest", (evt) => {
        // Inject author into outgoing form-encoded requests when not already set.
        const author = loadAuthor();
        if (!author) return;
        if (
            evt.detail.parameters &&
            (evt.detail.parameters.author === undefined ||
                evt.detail.parameters.author === "")
        ) {
            evt.detail.parameters.author = author;
        }
    });

    // ---- bootstrap ----------------------------------------------------------

    document.addEventListener("DOMContentLoaded", () => {
        setupAuthorInput();
        renderRecent();

        // If we're inside a project URL, remember it.
        const m = window.location.pathname.match(/^\/ui\/projects\/([^/]+)/);
        if (m) {
            pushRecent(decodeURIComponent(m[1]));
        }
    });
})();
