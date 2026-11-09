// 画面の一覧。
import {catalogPage} from "./catalog.js";
import {assetPage} from "./asset.js";
import {glossaryPage} from "./glossary.js";
import {lineagePage} from "./lineage.js";

export const routes = {
  catalog: catalogPage,
  assets: assetPage,
  glossary: glossaryPage,
  lineage: lineagePage,
};

export const navigation = [
  {href: "#/catalog", label: "nav.catalog", keys: ["catalog", "assets"]},
  {href: "#/glossary", label: "nav.glossary", keys: ["glossary"]},
  {href: "#/lineage", label: "nav.lineage", keys: ["lineage"]},
];
