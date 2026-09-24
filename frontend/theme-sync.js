/**
 * Gym AI Assistant — Universal Theme, Typography & Logo Synchronizer
 * Automatically synchronizes color themes, Google Fonts, and logo images across all pages:
 * /setup, /site, /chat, /leads, /config, and /admin.
 */
(function(window) {
  const PRESET_PALETTES = {
    emerald: {
      name: "Emerald Green",
      primary: "#16a34a",
      secondary: "#0f172a",
      accent: "#16a34a",
      accentDark: "#15803d",
      bgSoft: "#f8fafc",
      headerBg: "#0f172a",
      chatHeaderBg: "#075e54",
      userMsgBg: "#dcf8c6",
      userMsgText: "#111827",
      botMsgBg: "#ffffff",
      botMsgText: "#111827",
      font: "Inter"
    },
    cyberpunk: {
      name: "Cyberpunk Lime & Neon",
      primary: "#84cc16",
      secondary: "#0a0e17",
      accent: "#a3e635",
      accentDark: "#65a30d",
      bgSoft: "#0f172a",
      headerBg: "#0a0e17",
      chatHeaderBg: "#111827",
      userMsgBg: "#84cc16",
      userMsgText: "#000000",
      botMsgBg: "#1e293b",
      botMsgText: "#f8fafc",
      font: "Outfit"
    },
    sapphire: {
      name: "Midnight Sapphire",
      primary: "#2563eb",
      secondary: "#0b132b",
      accent: "#3b82f6",
      accentDark: "#1d4ed8",
      bgSoft: "#f8fafc",
      headerBg: "#0f172a",
      chatHeaderBg: "#1e3a8a",
      userMsgBg: "#dbeafe",
      userMsgText: "#1e3a8a",
      botMsgBg: "#ffffff",
      botMsgText: "#0f172a",
      font: "Montserrat"
    },
    crimson: {
      name: "Crimson Power",
      primary: "#dc2626",
      secondary: "#18181b",
      accent: "#ef4444",
      accentDark: "#b91c1c",
      bgSoft: "#fcfcfc",
      headerBg: "#18181b",
      chatHeaderBg: "#991b1b",
      userMsgBg: "#fee2e2",
      userMsgText: "#7f1d1d",
      botMsgBg: "#ffffff",
      botMsgText: "#18181b",
      font: "Outfit"
    },
    amber: {
      name: "Sunset Amber",
      primary: "#d97706",
      secondary: "#1c1917",
      accent: "#f59e0b",
      accentDark: "#b45309",
      bgSoft: "#fffbeb",
      headerBg: "#1c1917",
      chatHeaderBg: "#78350f",
      userMsgBg: "#fef3c7",
      userMsgText: "#78350f",
      botMsgBg: "#ffffff",
      botMsgText: "#1c1917",
      font: "Poppins"
    },
    purple: {
      name: "Royal Purple",
      primary: "#7c3aed",
      secondary: "#110b29",
      accent: "#8b5cf6",
      accentDark: "#6d28d9",
      bgSoft: "#faf5ff",
      headerBg: "#110b29",
      chatHeaderBg: "#5b21b6",
      userMsgBg: "#ede9fe",
      userMsgText: "#4c1d95",
      botMsgBg: "#ffffff",
      botMsgText: "#110b29",
      font: "Plus Jakarta Sans"
    },
    obsidian: {
      name: "Stealth Obsidian",
      primary: "#64748b",
      secondary: "#020617",
      accent: "#94a3b8",
      accentDark: "#475569",
      bgSoft: "#090d16",
      headerBg: "#020617",
      chatHeaderBg: "#0f172a",
      userMsgBg: "#334155",
      userMsgText: "#ffffff",
      botMsgBg: "#1e293b",
      botMsgText: "#f8fafc",
      font: "Space Grotesk"
    },
    teal: {
      name: "Aqua Teal",
      primary: "#0d9488",
      secondary: "#042f2e",
      accent: "#14b8a6",
      accentDark: "#0f766e",
      bgSoft: "#f0fdfa",
      headerBg: "#042f2e",
      chatHeaderBg: "#115e59",
      userMsgBg: "#ccfbf1",
      userMsgText: "#134e4a",
      botMsgBg: "#ffffff",
      botMsgText: "#042f2e",
      font: "DM Sans"
    },
    rose: {
      name: "Rose Gold",
      primary: "#e11d48",
      secondary: "#1a0a0e",
      accent: "#f43f5e",
      accentDark: "#be123c",
      bgSoft: "#fff1f2",
      headerBg: "#1a0a0e",
      chatHeaderBg: "#4c0519",
      userMsgBg: "#ffe4e6",
      userMsgText: "#4c0519",
      botMsgBg: "#ffffff",
      botMsgText: "#1a0a0e",
      font: "Poppins"
    },
    violet: {
      name: "Electric Violet",
      primary: "#8b5cf6",
      secondary: "#0f0a1a",
      accent: "#a78bfa",
      accentDark: "#7c3aed",
      bgSoft: "#f5f3ff",
      headerBg: "#0f0a1a",
      chatHeaderBg: "#4c1d95",
      userMsgBg: "#ede9fe",
      userMsgText: "#4c1d95",
      botMsgBg: "#ffffff",
      botMsgText: "#0f0a1a",
      font: "Plus Jakarta Sans"
    },
    indigo: {
      name: "Deep Indigo",
      primary: "#4f46e5",
      secondary: "#0a0a1a",
      accent: "#6366f1",
      accentDark: "#3730a3",
      bgSoft: "#eef2ff",
      headerBg: "#0a0a1a",
      chatHeaderBg: "#312e81",
      userMsgBg: "#e0e7ff",
      userMsgText: "#312e81",
      botMsgBg: "#ffffff",
      botMsgText: "#0a0a1a",
      font: "Montserrat"
    },
    ocean: {
      name: "Deep Ocean",
      primary: "#0284c7",
      secondary: "#082f49",
      accent: "#38bdf8",
      accentDark: "#0369a1",
      bgSoft: "#f0f9ff",
      headerBg: "#082f49",
      chatHeaderBg: "#075985",
      userMsgBg: "#e0f2fe",
      userMsgText: "#075985",
      botMsgBg: "#ffffff",
      botMsgText: "#082f49",
      font: "Raleway"
    },
    sunset: {
      name: "Warm Sunset",
      primary: "#ea580c",
      secondary: "#1c1917",
      accent: "#f97316",
      accentDark: "#c2410c",
      bgSoft: "#fff7ed",
      headerBg: "#1c1917",
      chatHeaderBg: "#7c2d12",
      userMsgBg: "#ffedd5",
      userMsgText: "#7c2d12",
      botMsgBg: "#ffffff",
      botMsgText: "#1c1917",
      font: "Outfit"
    },
    forest: {
      name: "Forest Green",
      primary: "#15803d",
      secondary: "#052e16",
      accent: "#22c55e",
      accentDark: "#166534",
      bgSoft: "#f0fdf4",
      headerBg: "#052e16",
      chatHeaderBg: "#14532d",
      userMsgBg: "#dcfce7",
      userMsgText: "#14532d",
      botMsgBg: "#ffffff",
      botMsgText: "#052e16",
      font: "Inter"
    },
    minimalist: {
      name: "Clean Minimalist",
      primary: "#171717",
      secondary: "#fafafa",
      accent: "#404040",
      accentDark: "#262626",
      bgSoft: "#ffffff",
      headerBg: "#171717",
      chatHeaderBg: "#262626",
      userMsgBg: "#f5f5f5",
      userMsgText: "#171717",
      botMsgBg: "#ffffff",
      botMsgText: "#171717",
      font: "Inter"
    }
  };

  const GOOGLE_FONTS = [
    "Inter",
    "Outfit",
    "Montserrat",
    "Roboto",
    "Poppins",
    "Plus Jakarta Sans",
    "DM Sans",
    "Space Grotesk",
    "Raleway",
    "Nunito",
    "Work Sans",
    "Josefin Sans"
  ];

  function getApiBase() {
    return window.GYM_AI_API_BASE || (
      window.location.origin && !window.location.origin.includes("null") && !window.location.origin.includes("file:")
        ? window.location.origin
        : "http://localhost:8000"
    );
  }

  // Default brand logo (Arivayya AI) when a gym hasn't uploaded its own
  const DEFAULT_LOGO = "/static/assets/arivayya-ai-logo.png";

  function getGymId() {
    // A signed-in gym owner always sees their own gym
    if (localStorage.getItem("gym_role") === "owner" && localStorage.getItem("gym_session_gym")) {
      return localStorage.getItem("gym_session_gym");
    }
    const params = new URLSearchParams(window.location.search);
    return params.get("gym") || localStorage.getItem("gym_id") || "tarvos-fit";
  }

  // Cache keys are per gym, so one gym's logo/name/theme never shows up for another
  function key(name) { return name + "_" + getGymId(); }

  function loadGoogleFont(fontName) {
    if (!fontName) return;
    const cleanFont = fontName.replace(/['"]/g, "").trim();
    const id = "gf-" + cleanFont.toLowerCase().replace(/\s+/g, "-");
    if (document.getElementById(id)) return;

    const link = document.createElement("link");
    link.id = id;
    link.rel = "stylesheet";
    link.href = `https://fonts.googleapis.com/css2?family=${encodeURIComponent(cleanFont)}:wght@300;400;500;600;700;800&display=swap`;
    document.head.appendChild(link);
  }

  function applyTheme(themeData, fontName, logoUrl, gymName) {
    const root = document.documentElement;
    const style = root.style;
    // Admin pages (setup, leads, gyms console) keep the Arivayya white & blue look;
    // gym colours and fonts only apply to the chat and public pages.
    const isAppPage = !!(document.body && document.body.dataset && document.body.dataset.appTheme);
    if (isAppPage) { themeData = null; fontName = null; }
    if (themeData) {
      const p = themeData.primary_color || themeData.primary;
      if (p) {
        style.setProperty("--primary", p);
        style.setProperty("--accent", p);
        style.setProperty("--accent-primary", p);
        style.setProperty("--button-bg", p);
      }
      const s = themeData.secondary_color || themeData.secondary;
      if (s) {
        style.setProperty("--secondary", s);
        style.setProperty("--bg-dark", s);
      }
      const h = themeData.chatbot_header_color || themeData.chatHeaderBg || s || p;
      if (h) {
        style.setProperty("--header-bg", h);
        style.setProperty("--wa-header-bg", h);
      }
      const u = themeData.user_msg_color || themeData.userMsgBg || p;
      if (u) {
        style.setProperty("--user-msg-bg", u);
        // If user bubble matches primary color, set user text to white for contrast
        if (!themeData.user_msg_text && u === p) {
          style.setProperty("--user-msg-text", "#ffffff");
        }
      }
      if (themeData.user_msg_text || themeData.userMsgText) {
        style.setProperty("--user-msg-text", themeData.user_msg_text || themeData.userMsgText);
      }
      if (themeData.bot_msg_color || themeData.botMsgBg) {
        style.setProperty("--bot-msg-bg", themeData.bot_msg_color || themeData.botMsgBg);
      }
      if (themeData.bot_msg_text || themeData.botMsgText) {
        style.setProperty("--bot-msg-text", themeData.bot_msg_text || themeData.botMsgText);
      }
      if (themeData.background_color || themeData.bgSoft) {
        style.setProperty("--bg-soft", themeData.background_color || themeData.bgSoft);
        style.setProperty("--bg-page", themeData.background_color || themeData.bgSoft);
      }
    }


    const finalFont = fontName || (themeData && (themeData.font_family || themeData.font));
    if (finalFont) {
      loadGoogleFont(finalFont);
      style.setProperty("--font-family", `'${finalFont}', -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif`);
      document.body.style.fontFamily = `var(--font-family)`;
    }

    const finalLogo = logoUrl || localStorage.getItem(key("gym_logo")) || DEFAULT_LOGO;
    if (finalLogo) {
      document.querySelectorAll("[data-gym-logo], .gym-logo-img, #headerAvatar img, .gymchat-head img, #welcomeLogoWrap img").forEach(img => {
        if (img.tagName === "IMG") {
          img.src = finalLogo;
          img.style.display = "inline-block";
        }
      });
      const brandLogo = document.getElementById("brandLogo");
      if (brandLogo) {
        brandLogo.innerHTML = `<img src="${finalLogo}" alt="${gymName || 'Gym Logo'}" style="width:100%;height:100%;object-fit:contain;border-radius:10px;" onerror="this.onerror=null;this.src='${DEFAULT_LOGO}'" />`;
      }
      // For chat avatar container
      const chatAvatar = document.getElementById("headerAvatar");
      if (chatAvatar) {
        chatAvatar.innerHTML = `<img src="${finalLogo}" alt="Logo" style="width:100%;height:100%;object-fit:cover;border-radius:50%;" /><span class="online-indicator"></span>`;
      }
      // For welcome message logo container in chat body
      const welcomeLogoWrap = document.getElementById("welcomeLogoWrap");
      if (welcomeLogoWrap) {
        welcomeLogoWrap.innerHTML = `<img src="${finalLogo}" alt="Logo" style="width:100%;height:100%;object-fit:contain;border-radius:8px;" />`;
      }
      // For embedded chat widget header logo on public site
      const chatHeadLogo = document.querySelector(".gymchat-head div span:first-child");
      if (chatHeadLogo && !chatHeadLogo.querySelector("img")) {
        chatHeadLogo.innerHTML = `<img src="${finalLogo}" style="width:24px;height:24px;object-fit:contain;background:#fff;border-radius:4px;padding:2px;" alt="Logo" />`;
      }
      // Update favicon
      let favicon = document.querySelector("link[rel~='icon']");
      if (!favicon) {
        favicon = document.createElement("link");
        favicon.rel = "icon";
        document.head.appendChild(favicon);
      }
      favicon.href = finalLogo;
    }

    const cleanGymName = (gymName || localStorage.getItem(key("gym_name")) || "").replace(/\s+Elite$/i, "");
    if (cleanGymName) {
      document.querySelectorAll("[data-gym-name], #gymNameDisplay, #welcomeGymName").forEach(el => {
        el.textContent = cleanGymName;
      });
    }

    window.dispatchEvent(new CustomEvent("gym-theme-updated", {
      detail: { theme: themeData, font: finalFont, logo: finalLogo, gymName: gymName }
    }));
  }

  function getLocalTheme() {
    try {
      const raw = localStorage.getItem(key("gym_theme_config"));
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  async function sync() {
    const gymId = getGymId();
    const local = getLocalTheme();
    const localFont = localStorage.getItem(key("gym_font")) || (local && local.font_family);
    const localLogo = localStorage.getItem(key("gym_logo"));
    const localName = localStorage.getItem(key("gym_name"));

    // Immediately apply this gym's cached look (or the default logo)
    applyTheme(local, localFont, localLogo, localName);

    // Then asynchronously pull from server to ensure fresh state
    try {
      const res = await fetch(`${getApiBase()}/api/gym/${gymId}/info`);
      if (res.ok) {
        const data = await res.json();
        const serverTheme = (data.theme && Object.keys(data.theme).length) ? data.theme : {};
        const mergedTheme = Object.keys(serverTheme).length ? { ...(local || {}), ...serverTheme } : (local || {});
        const serverFont = mergedTheme.font_family || mergedTheme.font || localFont || "Inter";
        const serverLogo = data.logo_url || DEFAULT_LOGO;
        const serverName = data.gym_name || localName;

        if (Object.keys(mergedTheme).length) {
          localStorage.setItem(key("gym_theme_config"), JSON.stringify(mergedTheme));
        }
        if (serverFont) localStorage.setItem(key("gym_font"), serverFont);
        if (data.logo_url) localStorage.setItem(key("gym_logo"), data.logo_url);
        else localStorage.removeItem(key("gym_logo"));   // logo removed on the server
        if (serverName) localStorage.setItem(key("gym_name"), serverName);

        applyTheme(mergedTheme, serverFont, serverLogo, serverName);
      }
    } catch (err) {
      console.warn("Theme-sync server fetch fallback:", err);
    }
  }


  // Hamburger Menu Component Initialization
  function initHamburgerMenu() {
    const hamburgerBtn = document.getElementById("hamburgerBtn") || document.querySelector(".hamburger-btn");
    const mobileDrawer = document.getElementById("mobileNavDrawer") || document.querySelector(".mobile-nav-drawer");
    const mobileOverlay = document.getElementById("mobileNavOverlay") || document.querySelector(".mobile-nav-overlay");

    if (hamburgerBtn && mobileDrawer) {
      function toggleMenu() {
        const isOpen = mobileDrawer.classList.toggle("open");
        hamburgerBtn.classList.toggle("active", isOpen);
        if (mobileOverlay) mobileOverlay.classList.toggle("open", isOpen);
        hamburgerBtn.setAttribute("aria-expanded", isOpen ? "true" : "false");
        document.body.style.overflow = isOpen ? "hidden" : "";
      }

      hamburgerBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        toggleMenu();
      });

      if (mobileOverlay) {
        mobileOverlay.addEventListener("click", () => {
          if (mobileDrawer.classList.contains("open")) toggleMenu();
        });
      }

      document.querySelectorAll(".mobile-nav-drawer a").forEach(link => {
        link.addEventListener("click", () => {
          if (mobileDrawer.classList.contains("open")) toggleMenu();
        });
      });
    }
  }

  // One-time clean-up of the old shared cache keys
  try {
    ["gym_logo", "gym_theme_config", "gym_font"].forEach(k => localStorage.removeItem(k));
  } catch (e) {}

  // Auto-init on DOMContentLoaded
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      sync();
      initHamburgerMenu();
    });
  } else {
    sync();
    initHamburgerMenu();
  }

  // Expose global API
  window.GymTheme = {
    PRESETS: PRESET_PALETTES,
    FONTS: GOOGLE_FONTS,
    apply: applyTheme,
    sync: sync,
    getApiBase: getApiBase,
    getGymId: getGymId,
    DEFAULT_LOGO: DEFAULT_LOGO,
    loadFont: loadGoogleFont,
    initHamburgerMenu: initHamburgerMenu
  };
})(window);
