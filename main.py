function doGet(e) {
  var p = e.parameter;
  if (p.action === 'slots') {
    return getSlots(p.date, p.master);
  }
  return createBooking(p);
}

function getSlots(dateStr, master) {
  var parts = dateStr.split('.');
  var date = new Date(parseInt(parts[2]), parseInt(parts[1])-1, parseInt(parts[0]));
  var nextDay = new Date(date.getTime() + 24*60*60*1000);

  var calendar = CalendarApp.getCalendarById('243cac82c8065a873bdf53632e7b910d9702b4aebce0d6868eab83c736d7084b@group.calendar.google.com');
  var events = calendar.getEvents(date, nextDay);

  var workStart = null;
  var workEnd = null;
  var busySlots = [];

  var masterFirst = master.split(' ')[0].toLowerCase();
  var allDayKeywords = ['весь день', 'целый день', 'full day'];
  var workKeywords = ['работаю', 'работает', 'смена', 'доступна', 'open'];
  var busyKeywords = ['занята', 'занят', 'недоступна', 'busy'];

  events.forEach(function(ev) {
    var title = ev.getTitle().toLowerCase();
    var hasMaster = title.indexOf(masterFirst) !== -1;
    var isBooking = title.indexOf('—') !== -1;
    var isWork = workKeywords.some(function(w){ return title.indexOf(w) !== -1; });
    var isBusy = busyKeywords.some(function(w){ return title.indexOf(w) !== -1; });
    var isAllDay = allDayKeywords.some(function(w){ return title.indexOf(w) !== -1; });

    if (!isBooking && !isBusy && (hasMaster || isWork)) {
      if (isAllDay) {
        // Весь день — стандартные часы 10:00–19:00
        workStart = 600;
        workEnd = 1140;
      } else {
        // Берём реальное время события
        workStart = ev.getStartTime().getHours() * 60 + ev.getStartTime().getMinutes();
        workEnd   = ev.getEndTime().getHours()   * 60 + ev.getEndTime().getMinutes();
      }
    } else if (isBooking || isBusy) {
      busySlots.push(ev.getStartTime().getHours() * 60 + ev.getStartTime().getMinutes());
    }
  });

  var available = [];
  if (workStart !== null && workEnd !== null) {
    for (var slot = workStart; slot + 30 <= workEnd; slot += 30) {
      if (busySlots.indexOf(slot) === -1) {
        var h = Math.floor(slot / 60);
        var m = slot % 60;
        available.push(String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0'));
      }
    }
  }

  return ContentService
    .createTextOutput(JSON.stringify({slots: available}))
    .setMimeType(ContentService.MimeType.JSON);
}

function createBooking(p) {
  try {
    var parts = p.date.split('.');
    var timeParts = p.time.split(':');
    var startTime = new Date(parseInt(parts[2]), parseInt(parts[1])-1, parseInt(parts[0]), parseInt(timeParts[0]), parseInt(timeParts[1]));
    var endTime = new Date(startTime.getTime() + 90*60*1000);

    var title = p.master + ' — ' + p.breed + ' — ' + p.service;
    var description =
      '🐾 БРОНИРОВАНИЕ R&J GROOMING\n\n' +
      '👤 Клиент: ' + p.name + '\n' +
      '📞 Телефон: ' + p.phone + '\n' +
      (p.pet ? '🐶 Кличка: ' + p.pet + '\n' : '') +
      '\n🐕 Порода: ' + p.breed + '\n' +
      '✂️ Услуга: ' + p.service + '\n' +
      '💰 Стоимость: ' + p.price + ' €\n' +
      '⏰ Последний грум: ' + p.groomHistory + '\n' +
      '👩 Мастер: ' + p.master;

    var calendar = CalendarApp.getCalendarById('243cac82c8065a873bdf53632e7b910d9702b4aebce0d6868eab83c736d7084b@group.calendar.google.com');
    var event = calendar.createEvent(title, startTime, endTime, {
      description: description,
      location: 'R&J Grooming, Таллин'
    });
    event.setColor(CalendarApp.EventColor.YELLOW);

    return ContentService
      .createTextOutput(JSON.stringify({success: true}))
      .setMimeType(ContentService.MimeType.JSON);
  } catch(err) {
    return ContentService
      .createTextOutput(JSON.stringify({success: false, error: err.toString()}))
      .setMimeType(ContentService.MimeType.JSON);
  }
}
