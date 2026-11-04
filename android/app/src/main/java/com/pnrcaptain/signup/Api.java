package com.pnrcaptain.signup;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Handler;
import android.os.Looper;

import org.json.JSONArray;
import org.json.JSONObject;
import org.json.JSONTokener;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Talks to the PNR Captain server (same Bearer token as the Chrome extension). */
public final class Api {
    public static final String DEFAULT_SERVER = "https://www.pnrcaptain.com";

    public interface Done { void ok(Object result); }
    public interface Fail { void error(String message); }

    private static final ExecutorService POOL = Executors.newSingleThreadExecutor();
    private static final Handler MAIN = new Handler(Looper.getMainLooper());

    private final Context ctx;

    public Api(Context ctx) { this.ctx = ctx.getApplicationContext(); }

    // ---------------------------------------------------------------- settings
    private SharedPreferences prefs() { return ctx.getSharedPreferences("pnr", Context.MODE_PRIVATE); }

    public String server() {
        String s = prefs().getString("server", DEFAULT_SERVER).trim();
        while (s.endsWith("/")) s = s.substring(0, s.length() - 1);
        return s.isEmpty() ? DEFAULT_SERVER : s;
    }
    public String token() { return prefs().getString("token", ""); }
    /** Optional: open this page instead of the server's register_url (e.g. a mock Register page). */
    public String testUrl() { return prefs().getString("test_url", "").trim(); }

    public void save(String server, String token, String testUrl) {
        prefs().edit().putString("server", server.trim()).putString("token", token.trim())
                .putString("test_url", testUrl.trim()).apply();
    }
    public void logout() { prefs().edit().remove("token").apply(); }

    // ---------------------------------------------------------------- HTTP
    public void get(String path, Done done, Fail fail) { call("GET", path, done, fail); }
    public void post(String path, Done done, Fail fail) { call("POST", path, done, fail); }

    private void call(String method, String path, Done done, Fail fail) {
        POOL.execute(() -> {
            try {
                Object r = request(method, path);
                MAIN.post(() -> done.ok(r));
            } catch (Exception e) {
                String msg = e.getMessage() == null ? e.toString() : e.getMessage();
                MAIN.post(() -> fail.error(msg));
            }
        });
    }

    private Object request(String method, String path) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(server() + path).openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(15000);
        c.setReadTimeout(30000);
        c.setRequestProperty("Authorization", "Bearer " + token());
        c.setRequestProperty("Accept", "application/json");
        if (method.equals("POST")) {
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            try (OutputStream o = c.getOutputStream()) { o.write("{}".getBytes(StandardCharsets.UTF_8)); }
        }
        int code = c.getResponseCode();
        InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
        String body = in == null ? "" : read(in);
        String type = c.getContentType() == null ? "" : c.getContentType();
        c.disconnect();
        if (code == 401) throw new Exception("Token wrong or revoked (401). Check it on the dashboard.");
        if (!type.contains("json")) {
            throw new Exception("Wrong server address: " + server() + " did not answer like the PNR API (HTTP "
                    + code + "). Use https://www.pnrcaptain.com, not the dashboard port.");
        }
        Object json = new JSONTokener(body).nextValue();
        if (code >= 400) {
            String detail = json instanceof JSONObject ? ((JSONObject) json).optString("detail",
                    ((JSONObject) json).optString("error", body)) : body;
            throw new Exception(detail + " (HTTP " + code + ")");
        }
        return json;
    }

    private static String read(InputStream in) throws Exception {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) b.write(buf, 0, n);
        in.close();
        return b.toString("UTF-8");
    }

    public static JSONArray arr(Object o) { return o instanceof JSONArray ? (JSONArray) o : new JSONArray(); }
    public static JSONObject obj(Object o) { return o instanceof JSONObject ? (JSONObject) o : new JSONObject(); }
}
