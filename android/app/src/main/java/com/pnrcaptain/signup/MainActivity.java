package com.pnrcaptain.signup;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.widget.ArrayAdapter;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/** Screen 1 (login: server + token) and screen 2 (pick a job). */
public class MainActivity extends Activity {
    private Api api;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        api = new Api(this);
        if (api.token().isEmpty()) showLogin(null);
        else showJobs();
    }

    private void showLogin(String error) {
        setTitle("PNR Signup — sign in");
        LinearLayout col = Ui.column(this);
        col.addView(Ui.title(this, "Connect to PNR Captain"));
        col.addView(Ui.small(this, "Use the same token as the Chrome extension (dashboard → Extension tokens)."));
        EditText server = Ui.input(this, "Server address", api.server(), false);
        EditText token = Ui.input(this, "Token", api.token(), true);
        EditText test = Ui.input(this, "Test page URL (optional, leave empty for saudia.com)", api.testUrl(), false);
        col.addView(Ui.text(this, "Server"));
        col.addView(server);
        col.addView(Ui.text(this, "Token"));
        col.addView(token);
        col.addView(Ui.text(this, "Test page (optional)"));
        col.addView(test);
        TextView status = Ui.small(this, error == null ? "" : error);
        status.setTextColor(0xFFB00020);
        col.addView(Ui.button(this, "Sign in", () -> {
            api.save(server.getText().toString(), token.getText().toString(), test.getText().toString());
            status.setTextColor(0xFF666666);
            status.setText("Checking…");
            api.get("/api/me", r -> showJobs(), msg -> {
                status.setTextColor(0xFFB00020);
                status.setText(msg);
            });
        }));
        col.addView(status);
        setContentView(col);
    }

    private void showJobs() {
        setTitle("PNR Signup — jobs");
        LinearLayout col = Ui.column(this);
        col.addView(Ui.title(this, "Pick a job"));
        TextView status = Ui.small(this, "Loading jobs…");
        col.addView(status);
        ListView list = new ListView(this);
        col.addView(list, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));
        LinearLayout bar = Ui.row(this);
        bar.addView(Ui.button(this, "Refresh", this::showJobs), Ui.weight());
        bar.addView(Ui.button(this, "Settings", () -> showLogin(null)), Ui.weight());
        bar.addView(Ui.button(this, "Sign out", () -> { api.logout(); showLogin(null); }), Ui.weight());
        col.addView(bar);
        setContentView(col);

        api.get("/api/signup/jobs", r -> {
            JSONArray jobs = Api.arr(r);
            List<String> labels = new ArrayList<>();
            List<JSONObject> items = new ArrayList<>();
            for (int i = 0; i < jobs.length(); i++) {
                JSONObject j = jobs.optJSONObject(i);
                if (j == null) continue;
                items.add(j);
                labels.add("#" + j.optInt("id") + "  " + j.optString("name"));
            }
            status.setText(items.isEmpty() ? "No jobs on this account yet." : items.size() + " jobs — tap one");
            list.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_list_item_1, labels));
            list.setOnItemClickListener((p, v, pos, id) -> {
                JSONObject j = items.get(pos);
                startActivity(new Intent(this, JobActivity.class)
                        .putExtra("job_id", j.optInt("id"))
                        .putExtra("job_name", j.optString("name")));
            });
        }, msg -> {
            if (msg.contains("401")) showLogin(msg);
            else status.setText("Could not load jobs: " + msg);
        });
    }
}
