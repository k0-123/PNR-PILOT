package com.pnrcaptain.signup;

import android.app.Activity;
import android.app.DownloadManager;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.Iterator;

/**
 * Screen 3: one job. Its signup batch = the job's green (found) rows, made on the server by
 * POST /api/jobs/{id}/signup-batch and named "Job #{id} {name}" — we find it again by that name.
 */
public class JobActivity extends Activity {
    private Api api;
    private int jobId;
    private String jobName;
    private int batchId = -1;
    private TextView status, counts;
    private Button start, make, excel;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        api = new Api(this);
        jobId = getIntent().getIntExtra("job_id", -1);
        jobName = getIntent().getStringExtra("job_name");
        setTitle("Job #" + jobId);

        LinearLayout col = Ui.column(this);
        col.addView(Ui.title(this, "#" + jobId + "  " + jobName));
        status = Ui.small(this, "Loading…");
        counts = Ui.text(this, "");
        col.addView(status);
        col.addView(counts);
        make = Ui.button(this, "Make signup list from green rows", this::makeBatch);
        start = Ui.button(this, "Start signups", () -> startActivity(
                new Intent(this, SignupActivity.class).putExtra("batch_id", batchId).putExtra("job_name", jobName)));
        excel = Ui.button(this, "Download Excel", this::downloadExcel);
        col.addView(make);
        col.addView(start);
        col.addView(excel);
        col.addView(Ui.button(this, "Refresh", this::load));
        col.addView(Ui.small(this, "Only the job's green rows (booking found) are signed up, with the "
                + "passenger's own name from the booking. Check every form before pressing Confirm."));
        setContentView(col);
    }

    @Override
    protected void onResume() {
        super.onResume();
        load();
    }

    private void load() {
        make.setEnabled(false);
        start.setEnabled(false);
        excel.setEnabled(false);
        String prefix = "Job #" + jobId + " ";
        api.get("/api/signup-batches", r -> {
            JSONArray list = Api.arr(r);
            batchId = -1;
            for (int i = 0; i < list.length(); i++) {   // newest first
                JSONObject b = list.optJSONObject(i);
                if (b != null && b.optString("name").startsWith(prefix)) { batchId = b.optInt("id"); break; }
            }
            if (batchId < 0) {
                status.setText("No signup list for this job yet.");
                counts.setText("");
                make.setEnabled(true);
                return;
            }
            status.setText("Signup list #" + batchId);
            api.get("/api/signup-batches/" + batchId + "/progress", p -> {
                JSONObject o = Api.obj(p);
                JSONObject c = o.optJSONObject("counts");
                StringBuilder s = new StringBuilder("Total " + o.optInt("total"));
                int pending = 0, busy = 0;
                if (c != null) {
                    for (Iterator<String> it = c.keys(); it.hasNext(); ) {
                        String k = it.next();
                        s.append("\n").append(k).append(": ").append(c.optInt(k));
                        if (k.equals("PENDING")) pending = c.optInt(k);
                        if (k.equals("FILLED")) busy = c.optInt(k);  // on another phone, or stuck (back after 15 min)
                    }
                }
                counts.setText(s.toString());
                start.setEnabled(pending + busy > 0);
                start.setText(pending > 0 ? "Start signups (" + pending + " waiting)"
                        : busy > 0 ? "Start (" + busy + " in progress on other phones)" : "Nothing waiting");
                excel.setEnabled(true);
            }, msg -> status.setText("Progress failed: " + msg));
        }, msg -> status.setText("Could not load: " + msg));
    }

    private void makeBatch() {
        make.setEnabled(false);
        status.setText("Making list…");
        api.post("/api/jobs/" + jobId + "/signup-batch", r -> {
            Toast.makeText(this, Api.obj(r).optInt("people") + " people added", Toast.LENGTH_SHORT).show();
            load();
        }, msg -> {
            status.setText(msg);
            make.setEnabled(true);
        });
    }

    private void downloadExcel() {
        String name = "alfursan_job_" + jobId + "_batch_" + batchId + ".xlsx";
        DownloadManager.Request req = new DownloadManager.Request(
                Uri.parse(api.server() + "/api/signup-batches/" + batchId + "/result.xlsx"))
                .addRequestHeader("Authorization", "Bearer " + api.token())
                .setTitle(name)
                .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
        ((DownloadManager) getSystemService(DOWNLOAD_SERVICE)).enqueue(req);
        Toast.makeText(this, "Downloading " + name, Toast.LENGTH_SHORT).show();
    }
}
