// 画面の一覧。
import {h} from "../lib/dom.js";
import {t} from "../lib/i18n.js";

export const routes = {
  catalog: () => h("h1", {}, t("nav.catalog")),
};

export const navigation = [
  {href: "#/catalog", label: "nav.catalog", keys: ["catalog", "assets"]},
];
