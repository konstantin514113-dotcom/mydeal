/**
 * R&J Grooming — Google Apps Script
 * Cancel / Reschedule booking functions
 *
 * HOW TO ADD:
 * 1. Open your existing Apps Script project (the one deployed as GOOGLE_SCRIPT).
 * 2. Paste the two functions below into any .gs file.
 * 3. Inside your existing doGet(e) function, add the two `if (action === ...)` blocks
 *    shown at the bottom of this file.
 * 4. Re-deploy (Deploy → Manage deployments → New version).
 *
 * Set CALENDAR_ID to your calendar's ID (found in Calendar Settings → Integrate calendar).
 * Use 'primary' for the default calendar.
 */

var CALENDAR_ID = 'primary'; // ← change if using a specific calendar

// ── Shared: find an event whose description or title contains the phone ────────
function _findEventByPhone(phone) {
  var cal = CalendarApp.getCalendarById(CALENDAR_ID);
  if (!cal) cal = CalendarApp.getDefaultCalendar();
  var now  = new Date();
  var horizon = new Date(now.getTime() + 120 * 24 * 60 * 60 * 1000); // 120 days ahead
  var events = cal.getEvents(now, horizon);
  var clean = phone.replace(/[\s\-()]/g, '');
  for (var i = 0; i < events.length; i++) {
    var ev = events[i];
    var haystack = (ev.getDescription() || '') + ' ' + (ev.getTitle() || '');
    if (haystack.indexOf(clean) !== -1) return ev;
  }
  return null;
}

// ── cancelBooking(phone) ───────────────────────────────────────────────────────
// Finds the nearest upcoming event whose description contains phone, deletes it.
// Returns: {success, cancelled: {title, start}} or {success: false, error}
function cancelBooking(phone) {
  var ev = _findEventByPhone(phone);
  if (!ev) return {success: false, error: 'Booking not found for: ' + phone};
  var info = {title: ev.getTitle(), start: ev.getStartTime().toISOString()};
  ev.deleteEvent();
  return {success: true, cancelled: info};
}

// ── rescheduleBooking(phone, newDate, newTime) ─────────────────────────────────
// Finds the event by phone, moves it to newDate (DD.MM.YYYY) + newTime (HH:MM).
// Returns: {success, rescheduled: {title, newStart}} or {success: false, error}
function rescheduleBooking(phone, newDate, newTime) {
  var ev = _findEventByPhone(phone);
  if (!ev) return {success: false, error: 'Booking not found for: ' + phone};

  var dp = newDate.split('.');          // ['05','06','2026']
  var tp = newTime.split(':');          // ['11','00']
  var newStart = new Date(
    parseInt(dp[2]),
    parseInt(dp[1]) - 1,
    parseInt(dp[0]),
    parseInt(tp[0]),
    parseInt(tp[1]),
    0
  );
  var duration = ev.getEndTime().getTime() - ev.getStartTime().getTime();
  var newEnd   = new Date(newStart.getTime() + duration);

  ev.setTime(newStart, newEnd);
  return {
    success: true,
    rescheduled: {title: ev.getTitle(), newStart: newStart.toISOString()}
  };
}

// ── getBookingsForDate(dateStr) ───────────────────────────────────────────────
// Returns all calendar events on dateStr (DD.MM.YYYY) with phone numbers extracted
// from event descriptions. Used by /cron/reminders to send SMS reminders.
// Returns: {success, date, count, bookings: [{title, time, phone, master, lang}]}
function getBookingsForDate(dateStr) {
  var cal = CalendarApp.getCalendarById(CALENDAR_ID) || CalendarApp.getDefaultCalendar();
  var dp    = dateStr.split('.');  // ['05','06','2026']
  var start = new Date(parseInt(dp[2]), parseInt(dp[1]) - 1, parseInt(dp[0]), 0,  0,  0);
  var end   = new Date(parseInt(dp[2]), parseInt(dp[1]) - 1, parseInt(dp[0]), 23, 59, 59);
  var events = cal.getEvents(start, end);
  var tz = Session.getScriptTimeZone();

  var bookings = [];
  for (var i = 0; i < events.length; i++) {
    var ev   = events[i];
    var desc = ev.getDescription() || '';
    var time = Utilities.formatDate(ev.getStartTime(), tz, 'HH:mm');

    // Extract phone: E.164 format (+372XXXXXXX) from description
    var phoneMatch  = desc.match(/\+\d{7,15}/);
    var phone       = phoneMatch ? phoneMatch[0] : '';

    // Extract master (looks for "Мастер: Name" or "master: Name" in description)
    var masterMatch = desc.match(/[Мм]астер[:\s]+([^\n,]+)/);
    var master      = masterMatch ? masterMatch[1].trim() : '';

    // Detect language
    var lang = 'ru';
    if (desc.indexOf('lang: et') !== -1 || desc.indexOf('"lang":"et"') !== -1) lang = 'et';
    else if (desc.indexOf('lang: en') !== -1 || desc.indexOf('"lang":"en"') !== -1) lang = 'en';

    bookings.push({
      title:  ev.getTitle(),
      time:   time,
      phone:  phone,
      master: master,
      lang:   lang,
    });
  }
  return {success: true, date: dateStr, count: bookings.length, bookings: bookings};
}

// ── doGet additions ────────────────────────────────────────────────────────────
// Add these blocks inside your existing doGet(e) function,
// before the default response (e.g. before the slots/days/book handling):
//
// var action = (e.parameter.action || '').toLowerCase();
//
// if (action === 'cancel') {
//   var res = cancelBooking(e.parameter.phone || '');
//   return ContentService.createTextOutput(JSON.stringify(res))
//            .setMimeType(ContentService.MimeType.JSON);
// }
//
// if (action === 'reschedule') {
//   var res = rescheduleBooking(
//     e.parameter.phone   || '',
//     e.parameter.newDate || '',
//     e.parameter.newTime || ''
//   );
//   return ContentService.createTextOutput(JSON.stringify(res))
//            .setMimeType(ContentService.MimeType.JSON);
// }
//
// if (action === 'bookings') {
//   var res = getBookingsForDate(e.parameter.date || '');
//   return ContentService.createTextOutput(JSON.stringify(res))
//            .setMimeType(ContentService.MimeType.JSON);
// }
