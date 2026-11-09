// IdP からのリダイレクト先 (ポップアップの中)。認可コードを本体のページに渡して閉じる。
// ストレージは使わず、本体のページのメモリにある verifier と state で検証する。
const query = new URLSearchParams(window.location.search);
const message = {code: query.get("code"), state: query.get("state"), error: query.get("error")};
history.replaceState(null, "", window.location.pathname); // コードを URL に残さない
const channel = new BroadcastChannel("edgf-oidc");
channel.postMessage(message);
channel.close();
document.getElementById("message").textContent = "OK";
window.close();
