# CAO UI Bug Fixes & Improvements

## Priority 1: Critical Fixes

### 1. Agent Name Not Showing in Sessions
- **Issue**: Active sessions show "General Agent" instead of actual agent name (e.g., "ticket-ninja")
- **Fix**: Display `session.agent_name` properly, show agent type badge based on actual name

### 2. Auto Mode Not Working
- **Issue**: When "Auto" is toggled on a session, nothing happens
- **Fix**: Auto mode should:
  - Automatically find highest priority unassigned bead
  - Assign it to the session
  - Begin working on it (send to agent)

### 3. Idle vs Waiting State
- **Issue**: No visual difference between IDLE and WAITING_INPUT states
- **Fix**: Merge these states or make WAITING_INPUT more prominent (it means agent needs user input)

## Priority 2: UX Improvements

### 4. Bead Assignment UX
- **Issue**: Dropdown for assigning beads is confusing - user doesn't know if assignment worked
- **Fix**: Replace dropdown with modal that:
  - Shows available sessions (waiting/unassigned)
  - Confirms assignment with visual feedback
  - Shows success/error state

### 5. View Bead from Session
- **Issue**: Can't see bead details from the active session view
- **Fix**: Show assigned bead info in session card, clickable to expand

### 6. Hyperlink URLs in Beads
- **Issue**: URLs in bead descriptions are plain text
- **Fix**: Auto-detect and linkify URLs in bead title/description

### 7. Ralph Loop Creation
- **Issue**: Cannot create Ralph loops from UI
- **Fix**: Add "Start Ralph Loop" button/modal in Ralph tab

## Priority 3: Branding & Styling

### 8. Header Branding
- **Issue**: Shows "CAO Dashboard" with emoji
- **Fix**: 
  - Add AWS logo (SVG) on top left
  - Change name to "Messaging Agent Orchestrator"
  - Remove emoji from header

### 9. Footer Attribution
- **Issue**: Footer doesn't have author credit
- **Fix**: Add "Built by @abducabd" on bottom left

### 10. Replace Emojis with Icons
- **Issue**: Emojis look unprofessional
- **Fix**: Use react-icons library (Lucide or Heroicons) for all icons
  - Install: `npm install lucide-react`
  - Replace all emoji usage with proper SVG icons

### 11. Activity Feed Icons Too Small
- **Issue**: Icons in activity feed are tiny
- **Fix**: Increase icon size to 20-24px

## Implementation Order
1. Install lucide-react icons
2. Fix agent name display
3. Fix auto mode functionality  
4. Improve bead assignment modal
5. Add URL hyperlinking
6. Update branding (header/footer)
7. Replace all emojis with icons
8. Add Ralph loop creation UI
