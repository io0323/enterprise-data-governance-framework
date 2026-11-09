// 文言の辞書。ja と en に分け、既定は ja。
const dictionaries = {};
let language = "ja";

export async function loadDictionaries() {
  for (const lang of ["ja", "en"]) {
    const response = await fetch(`./i18n/${lang}.json`, {method: "GET"});
    dictionaries[lang] = await response.json();
  }
}

export function currentLanguage() {
  return language;
}

export function setLanguage(lang) {
  language = lang;
  document.documentElement.lang = lang;
}

export function t(key, vars) {
  let text = dictionaries[language]?.[key] ?? key;
  for (const [name, value] of Object.entries(vars || {})) {
    text = text.replaceAll(`{${name}}`, String(value));
  }
  return text;
}
