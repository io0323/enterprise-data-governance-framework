// 画面の一覧。
import {dashboardPage} from "./dashboard.js";
import {catalogPage} from "./catalog.js";
import {assetPage} from "./asset.js";
import {glossaryPage} from "./glossary.js";
import {lineagePage} from "./lineage.js";
import {productsPage} from "./products.js";
import {issuesPage} from "./issues.js";
import {inboxPage} from "./inbox.js";
import {reviewsPage} from "./reviews.js";
import {maturityPage} from "./maturity.js";
import {jobsPage} from "./jobs.js";

export const routes = {
  dashboard: dashboardPage,
  catalog: catalogPage,
  assets: assetPage,
  glossary: glossaryPage,
  lineage: lineagePage,
  products: productsPage,
  issues: issuesPage,
  inbox: inboxPage,
  reviews: reviewsPage,
  maturity: maturityPage,
  jobs: jobsPage,
};

export const navigation = [
  {href: "#/catalog", label: "nav.catalog", keys: ["catalog", "assets"]},
  {href: "#/glossary", label: "nav.glossary", keys: ["glossary"]},
  {href: "#/lineage", label: "nav.lineage", keys: ["lineage"]},
  {href: "#/dashboard", label: "nav.dashboard", keys: ["dashboard"]},
  {href: "#/maturity", label: "nav.maturity", keys: ["maturity"]},
  {href: "#/products", label: "nav.products", keys: ["products"]},
  {href: "#/issues", label: "nav.issues", keys: ["issues"]},
  {href: "#/inbox", label: "nav.inbox", keys: ["inbox"]},
  {href: "#/reviews", label: "nav.reviews", keys: ["reviews"]},
  {href: "#/jobs", label: "nav.jobs", keys: ["jobs"]},
];
