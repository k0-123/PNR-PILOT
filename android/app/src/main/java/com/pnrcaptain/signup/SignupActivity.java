package com.pnrcaptain.signup;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.graphics.Bitmap;
import android.os.Bundle;
import android.text.method.ScrollingMovementMethod;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Screen 4: the Register page in a WebView. For each person: load page → fill (fill.js) → staff check
 * and press the site's own Confirm/Submit → "Confirmed → next" marks SUBMITTED on the server.
 * Every step goes to a log (Share log) so failures can be traced row by row.
 */
public class SignupActivity extends Activity {
    private static final String ERRORS_JS =
            "(function(){var o=[];document.querySelectorAll('mat-error,.mat-mdc-form-field-error,[role=alert],"
            + ".error,.error-message,.invalid-feedback').forEach(function(e){var t=(e.textContent||'')"
            + ".replace(/\\s+/g,' ').trim();if(t&&(e.offsetParent!==null||e.getClientRects().length)&&o.indexOf(t)<0)"
            + "o.push(t.slice(0,200))});return JSON.stringify({errors:o.slice(0,10),url:location.href});})()";

    private Api api;
    private int batchId;
    private WebView web;
    private TextView who, result, log;
    private Button again, skip, confirm;
    private JSONObject person;
    private boolean autoFilled;
    private String fillJs;
    private File logFile;
    private int done, skipped;

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        api = new Api(this);
        batchId = getIntent().getIntExtra("batch_id", -1);
        setTitle("Signup — " + getIntent().getStringExtra("job_name"));
        logFile = new File(getFilesDir(), "signup-log.txt");
        fillJs = asset("fill.js");

        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        int p = Ui.dp(this, 8);
        who = Ui.text(this, "Loading next person…");
        who.setPadding(p, p, p, 0);
        result = Ui.small(this, "");
        result.setPadding(p, 0, p, 0);
        col.addView(who);
        col.addView(result);

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);
        web.addJavascriptInterface(new Bridge(), "PNRApp");
        web.setWebViewClient(new Client());
        col.addView(web, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));

        log = Ui.small(this, "");
        log.setMaxLines(3);
        log.setMovementMethod(new ScrollingMovementMethod());
        log.setPadding(p, 0, p, 0);
        col.addView(log);

        LinearLayout bar = Ui.row(this);
        again = Ui.button(this, "Fill again", this::fill);
        skip = Ui.button(this, "Skip", () -> mark("SKIPPED"));
        confirm = Ui.button(this, "Confirmed → next", this::confirmed);
        bar.addView(again, Ui.weight());
        bar.addView(skip, Ui.weight());
        bar.addView(confirm, Ui.weight());
        col.addView(bar);
        LinearLayout bar2 = Ui.row(this);
        bar2.addView(Ui.button(this, "Share log", this::shareLog), Ui.weight());
        bar2.addView(Ui.button(this, "Stop", this::stop), Ui.weight());
        col.addView(bar2);
        setContentView(col);

        line("=== start batch " + batchId + " ===");
        next();
    }

    // ---------------------------------------------------------------- flow
    private void buttons(boolean on) {
        again.setEnabled(on);
        skip.setEnabled(on);
        confirm.setEnabled(on);
    }

    private void next() {
        buttons(false);
        person = null;
        api.post("/api/signup-batches/" + batchId + "/next", r -> {
            JSONObject p = Api.obj(r).optJSONObject("person");
            if (p == null) {
                who.setText("All done — " + done + " confirmed, " + skipped + " skipped.");
                result.setText("Nothing waiting in this list. Activation numbers arrive by email on the server.");
                line("batch finished: confirmed=" + done + " skipped=" + skipped);
                web.loadUrl("about:blank");
                return;
            }
            person = p;
            autoFilled = false;
            who.setText("#" + p.optInt("n") + "  " + p.optString("title") + " " + p.optString("first_name") + " "
                    + p.optString("last_name") + "\n" + p.optString("email"));
            result.setText("Opening Register page…");
            String url = api.testUrl().isEmpty() ? p.optString("register_url") : api.testUrl();
            line("person " + p.optInt("person_id") + " #" + p.optInt("n") + " " + p.optString("last_name")
                    + " → " + url);
            web.loadUrl(url);
            buttons(true);
        }, msg -> {
            who.setText("Server error");
            result.setText(msg);
            line("next failed: " + msg);
        });
    }

    private void fill() {
        if (person == null) return;
        autoFilled = true;
        result.setText("Filling…");
        web.evaluateJavascript(fillJs.replace("__DATA__", person.toString()), null);
    }

    private void confirmed() {
        if (person == null) return;
        // Look for red errors left on the page before marking the person as submitted.
        web.evaluateJavascript(ERRORS_JS, v -> {
            JSONArray errs = new JSONArray();
            try { errs = new JSONObject(new JSONArray("[" + v + "]").getString(0)).optJSONArray("errors"); }
            catch (Exception ignored) {}
            if (errs != null && errs.length() > 0) {
                line("page errors at confirm: " + errs);
                new AlertDialog.Builder(this)
                        .setTitle("The page still shows errors")
                        .setMessage(join(errs) + "\n\nMark this person as submitted anyway?")
                        .setPositiveButton("Yes, submitted", (d, w) -> mark("SUBMITTED"))
                        .setNegativeButton("No, go back", null)
                        .show();
            } else {
                mark("SUBMITTED");
            }
        });
    }

    private void mark(String status) {
        if (person == null) return;
        buttons(false);
        int id = person.optInt("person_id");
        api.post("/api/signup-people/" + id + "/" + status, r -> {
            line("person " + id + " → " + status);
            if (status.equals("SUBMITTED")) done++;
            if (status.equals("SKIPPED")) skipped++;
            next();
        }, msg -> {
            buttons(true);
            result.setText("Could not save: " + msg);
            line("mark " + status + " failed: " + msg);
        });
    }

    /** Leave: put the current (filled but not confirmed) person back in the queue. */
    private void stop() {
        if (person == null) { finish(); return; }
        int id = person.optInt("person_id");
        person = null;
        api.post("/api/signup-people/" + id + "/PENDING", r -> { line("person " + id + " back to PENDING"); finish(); },
                msg -> { line("stop failed: " + msg); finish(); });
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else stop();
    }

    // ---------------------------------------------------------------- WebView
    private class Client extends WebViewClient {
        @Override
        public void onPageStarted(WebView v, String url, Bitmap icon) {
            if (person != null) result.setText("Loading " + url);
        }

        @Override
        public void onPageFinished(WebView v, String url) {
            if (person == null || url.startsWith("about:")) return;
            if (!autoFilled) fill();
            else result.setText("Page: " + url);
        }

        @Override
        public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
            if (req.isForMainFrame()) {
                String m = "page failed: " + err.getDescription() + " (" + req.getUrl() + ")";
                result.setText(m);
                line(m);
            }
        }
    }

    private class Bridge {
        @JavascriptInterface
        public void done(String json) {
            runOnUiThread(() -> {
                try {
                    JSONObject o = new JSONObject(json);
                    StringBuilder s = new StringBuilder();
                    if (o.optBoolean("blocked")) s.append("BLOCKED by bot protection. ");
                    s.append("Filled: ").append(join(o.optJSONArray("filled")));
                    JSONArray miss = o.optJSONArray("missing");
                    if (miss != null && miss.length() > 0) s.append("\nNOT found: ").append(join(miss));
                    JSONArray errs = o.optJSONArray("errors");
                    if (errs != null && errs.length() > 0) s.append("\nPage says: ").append(join(errs));
                    s.append("\nCheck the form, press the site's Confirm, then \"Confirmed → next\".");
                    result.setText(s.toString());
                    line("fill: " + json);
                } catch (Exception e) {
                    line("bad fill answer: " + json);
                }
            });
        }
    }

    // ---------------------------------------------------------------- log
    private void line(String text) {
        String stamp = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(new Date());
        String l = stamp + "  " + text + "\n";
        log.append(l);
        try (FileOutputStream o = new FileOutputStream(logFile, true)) {
            o.write(l.getBytes(StandardCharsets.UTF_8));
        } catch (Exception ignored) {}
    }

    private void shareLog() {
        String text;
        try (InputStream in = new java.io.FileInputStream(logFile)) { text = read(in); }
        catch (Exception e) { text = log.getText().toString(); }
        if (text.length() > 90000) text = text.substring(text.length() - 90000);
        startActivity(Intent.createChooser(new Intent(Intent.ACTION_SEND).setType("text/plain")
                .putExtra(Intent.EXTRA_SUBJECT, "PNR Signup log").putExtra(Intent.EXTRA_TEXT, text), "Share log"));
    }

    private String asset(String name) {
        try (InputStream in = getAssets().open(name)) { return read(in); }
        catch (Exception e) { throw new RuntimeException(e); }
    }

    private static String read(InputStream in) throws Exception {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) b.write(buf, 0, n);
        return b.toString("UTF-8");
    }

    private static String join(JSONArray a) {
        if (a == null || a.length() == 0) return "nothing";
        StringBuilder s = new StringBuilder();
        for (int i = 0; i < a.length(); i++) s.append(i == 0 ? "" : ", ").append(a.optString(i));
        return s.toString();
    }
}
