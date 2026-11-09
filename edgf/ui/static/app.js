// エントリポイント: 共通のレイアウト、認証、ルーティング。
import {h} from "./lib/dom.js";
import {currentLanguage, loadDictionaries, setLanguage, t} from "./lib/i18n.js";
import * as oidc from "./lib/oidc.js";
import {parseHash} from "./lib/router.js";
import {problemView} from "./lib/components.js";
import {routes, navigation} from "./pages/index.js";

const root = document.getElementById("app");
let notice = null;

function skipLink() {
  return h("a", {class: "skip-link", href: "#main", onClick: (event) => {
    event.preventDefault(); // ハッシュのルーティングと衝突させない
    document.getElementById("main").focus();
  }}, t("nav.skip"));
}

function languageSwitch() {
  const next = currentLanguage() === "ja" ? "en" : "ja";
  return h("button", {type: "button", lang: next, onClick: () => {
    setLanguage(next);
    render();
  }}, t("nav.language"));
}

function header() {
  return h("header", {class: "site-header"},
    h("p", {class: "site-title"}, t("app.title")),
    h("div", {class: "site-actions"},
      languageSwitch(),
      oidc.userName() ? h("span", {class: "user"}, oidc.userName()) : null,
      oidc.accessToken() ? h("button", {type: "button", onClick: () => oidc.logout()}, t("nav.logout")) : null));
}

function currentKey() {
  return parseHash().segments[0] || "catalog";
}

function nav() {
  const active = currentKey();
  return h("nav", {class: "site-nav", "aria-label": t("nav.main")},
    h("ul", {}, navigation.map((item) => h("li", {},
      h("a", {href: item.href, "aria-current": item.keys.includes(active) ? "page" : null}, t(item.label))))));
}

function loginView() {
  const status = h("p", {role: "status", class: "muted"}, notice ? t(notice) : "");
  return h("div", {class: "login"},
    h("h1", {}, t("login.title")),
    h("p", {}, t("login.description")),
    status,
    h("button", {type: "button", class: "primary", id: "login", onClick: async () => {
      status.textContent = t("login.waiting");
      try {
        await oidc.login();
        notice = null;
        render();
      } catch (error) {
        status.textContent = t(`login.error.${error.message}`);
      }
    }}, t("login.button")));
}

function renderPage(main) {
  const {segments, params} = parseHash();
  const route = routes[segments[0] || "catalog"];
  if (!route) {
    main.replaceChildren(h("h1", {}, t("error.not_found")));
    return;
  }
  try {
    main.replaceChildren(route(segments.slice(1), params));
  } catch (error) {
    main.replaceChildren(problemView(error));
  }
  const heading = main.querySelector("h1");
  document.title = heading ? `${heading.textContent} - ${t("app.title")}` : t("app.title");
}

function render() {
  const main = h("main", {id: "main", tabindex: "-1"});
  if (oidc.accessToken()) {
    root.replaceChildren(skipLink(), header(), nav(), main);
    renderPage(main);
  } else {
    root.replaceChildren(skipLink(), header(), main);
    main.append(loginView());
    document.title = t("app.title");
  }
}

function onRouteChange() {
  render();
  document.getElementById("main")?.focus(); // 画面の切り替えをキーボードと支援技術の利用者に伝える
}

async function start() {
  await loadDictionaries();
  setLanguage("ja");
  const configured = await oidc.initialize(() => {
    notice = "login.expired"; // 401 または期限切れ: 再ログインを求める
    render();
  });
  if (!configured) {
    root.replaceChildren(h("main", {id: "main", class: "login"}, h("h1", {}, t("app.title")), h("p", {role: "alert"}, t("login.not_configured"))));
    return;
  }
  window.addEventListener("hashchange", onRouteChange);
  render();
}

start();
