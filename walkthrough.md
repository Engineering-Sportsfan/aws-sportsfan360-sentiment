# Walkthrough: Dolly Upgrade & Matches Table Integration

This walkthrough details the verification steps, local manual testing procedures, and generated question examples for the Dolly bot and dashboard upgrade.

## Verified Changes

### 1. Vercel Backend Admin Console (`sportsfan` on Port 3001)
* **Add Focus Match Page:** [Add Match Form](file:///Users/prishadureja/Desktop/sportsfan/app/admin/focusmatch-management/add-focusmatch/page.tsx) verified. Writes to Firestore `matches` collection via the backend API.
* **Matches List Page:** [Matches Tab Overview](file:///Users/prishadureja/Desktop/sportsfan/app/admin/focusmatch-management/focusmatch-list/page.tsx) verified. Displays matches separated into "Football Matches" and "Cricket Matches" tabs.
* **Matches API Route:** [Matches API endpoint](file:///Users/prishadureja/Desktop/sportsfan/app/api/roar/matches/route.ts) verified. Handles GET and POST requests.
* **Create RoAR Show form:** [AddRoarForm Component](file:///Users/prishadureja/Desktop/sportsfan/app/admin/roar-management/add-roar/AddRoarForm.tsx) verified. Contains the optional dropdown to link a RoAR room to a scheduled match.
* **Sidebar Layout:** [Sidebar Component](file:///Users/prishadureja/Desktop/sportsfan/app/admin/layout.tsx) verified. Includes "Focus Group Matches" navigation options.

### 2. Dolly Bot & Background Scheduler (`sportsfan360-sentiment` on Port 8000)
* **Automated pre-match LLM research:** [Research Pipeline Module](file:///Users/prishadureja/Desktop/sportsfan360-sentiment/research_pipeline.py) verified. Automatically queries Gemini with Google Search grounding to populate 4 storytelling pillars.
* **Matches live gating:** [Dolly Bot Module](file:///Users/prishadureja/Desktop/sportsfan360-sentiment/dolly_bot.py) verified. Gated to matches with `status: "live"`.
* **15-minute intervals:** [Main Scheduler Module](file:///Users/prishadureja/Desktop/sportsfan360-sentiment/main.py) verified. Checks loop frequency set to 15 minutes.

---

## Dolly Question Generation Examples

Here are examples of Dolly's upgraded questions based on the 4 storytelling pillars:

### Example 1: Football Match (Rivalry & Character Focus)
* **Debate:** `"With Carvajal's aggressive marking style, can Mbappe break free to dictate France's counter-attack tonight?"`
  - *Options:* `Carvajal wins` vs `Mbappe breaks free`
* **Prediction:** `"Will Mbappe score in the second half of this knockout match?"`
  - *Options:* `Yes` vs `No`

### Example 2: Cricket Match (Stats & Storytelling Focus)
* **Debate:** `"With two early wickets down in the powerplay, should India consolidate their innings or continue attacking?"`
  - *Options:* `Consolidate` vs `Keep attacking`
* **Prediction:** `"Will India score 50 or more runs by the end of the 6-over powerplay?"`
  - *Options:* `Yes` vs `No`

---

## How to Verify Locally on Your Laptop

Follow these steps to verify the entire system end-to-end on your localhost:

### Step 1: Open the Admin Console (Backend)
1. Open your browser and go to: **`http://localhost:3001/admin`** (this is the admin dashboard where you manage everything).
2. Log in using your email/account.
3. In the sidebar under **Home Data Components**, click **Matches List**. You will see the tabs: *Football Matches* and *Cricket Matches*.

### Step 2: Add a Test Match
1. In the sidebar, click **Add Match** (under Focus Group Matches).
2. Fill out the form:
   - **Sport:** Choose `Football` or `Cricket`.
   - **Teams:** e.g., *"Spain vs France"*
   - **Start Time:** Set a datetime.
   - **Status:** Set status to **`Live`** (to bypass the scheduler gating check).
3. Click **Add Focus Match**.
4. Check the **Matches List** page to verify it appears in the correct sport tab.

### Step 3: Create a Linked RoAR Room
1. In the sidebar, click **Add RoAR Show** (under RoAR Management).
2. Fill out the form. 
3. In the **Link to Focus Group Match** dropdown, you will see your newly created match (*Spain vs France*). **Select it.**
4. Click **Create Show Room**.

### Step 5: Run the Dolly Bot Trigger
1. Run a manual API call in your terminal to trigger Dolly:
   ```bash
   curl -X POST http://localhost:8000/run-dolly
   ```
2. Go to **`http://localhost:3000`** (the user frontend app) and enter the room.
3. **Verify:** You will see Dolly's storytelling questions posted instantly inside the messages, reflecting the teams you set!
