# Test coverage against the §16 scenario table

This maps every row of the spec's §16 "Testplan och acceptanskriterier" scenario
table (`home-assistant-avgangsplan.md`) to the test(s) that demonstrate it, and
records which rows are out of v1 scope. It is produced for task **T21**.

All tests use controlled time (`freezegun` / `async_fire_time_changed`) and
sanitised fixtures; nothing hits the network by default (the one SL live smoke
test is skipped unless `FD_LIVE_SL=1`). Run with `uv run pytest -q`.

Test ids below are `pytest` node names (`<file>::<test>`). Where a behaviour is
proven both as a focused unit test and inside a full-morning flow, both are
listed; the end-to-end tests live in `tests/test_e2e_morning.py` (T21).

## §16 scenario table

| § Scenario (spec text, abbreviated) | Test(s) | Notes |
| --- | --- | --- |
| Två lektioner och en uppgift tidigt – första faktiska lektionen väljs | `test_schedule.py::test_first_real_lesson_chosen_from_whole_day`; `test_e2e_morning.py::test_full_morning_transit_lifecycle` | First event of the whole local day wins |
| Första lektionen passerad, kvar hemma – sen status, ingen ny promotion | `test_schedule.py::test_passed_first_lesson_does_not_promote_later_lesson` | |
| Första lektionen inställd före avresa – nästa giltiga blir dagens första | `test_schedule.py::test_disappeared_first_lesson_moves_to_next_before_departure`; `test_provider_ics.py::test_disappeared_uids` | |
| Samma ändring efter avresa – inga nya lämna-hemmet-notiser | `test_schedule.py::test_locked_after_departure_keeps_first_event_despite_change`; `test_notification_policy.py::test_no_intents_after_departed_except_cleanup`; `test_coordinator.py::test_confirmed_departure_not_reset` | Day locks after departure |
| RRULE + EXDATE + flyttad RECURRENCE-ID – rätt lokalt dagsresultat | `test_provider_ics.py::test_rrule_exdate_and_recurrence_id`; `test_provider_ha_calendar.py::test_edited_single_occurrence`, `::test_deleted_occurrence_is_empty` | |
| SchoolSoft `TZID=Europe/Berlin`, sommar och vinter – rätt lokal starttid | `test_provider_ics.py::test_berlin_tzid_summer_local_time`, `::test_berlin_tzid_winter_local_time` | DST both sides |
| Dagens första SchoolSoft-lektion saknas i ny hämtning (UID borta) | `test_provider_ics.py::test_disappeared_uids`; `test_schedule.py::test_disappeared_first_lesson_moves_to_next_before_departure` | Flagged as possible cancellation |
| `Lektion LUNCH` tidigast – exkluderas, nästa väljs | `test_provider_ics.py::test_lunch_excluded_by_default_filter` | |
| Oförändrat flöde (samma hash) – ingen ny parsning/omberäkning | `test_provider_ics.py::test_unchanged_hash_skips_reparse` | |
| Idrott som tredje lektion – "Gympakläder" i kvälls- och morgonnotis, avgång oförändrad | `test_packing.py::test_pe_as_third_lesson_yields_item`; `test_notification_policy.py::test_evening_includes_tomorrow_packing`; `test_e2e_morning.py::test_evening_summary_and_packing_fire_the_night_before` | |
| Två idrottslektioner samma dag – en rad i packlistan | `test_packing.py::test_two_pe_lessons_collapse_to_one_item` | |
| Idrott tas bort efter kvällsnotisen – raden försvinner | `test_packing.py::test_removed_lesson_removes_item`, `::test_acknowledgement_trimmed_when_lesson_removed` | |
| Packlista kvitterad, sedan 10-minuterspåminnelse – påminnelsen nämner den inte | `test_notification_policy.py::test_packing_absent_after_acknowledgement` | |
| Dagsundantag `sick` på idrottsdag – ingen packpåminnelse | `test_packing.py::test_sick_override_empties_list` | |
| Kvitterat "Packat" på kvällen – morgonnotisen nämner inte gympakläderna | `test_notification_policy.py::test_packing_absent_after_acknowledgement`; `test_dispatcher.py::test_packed_action_acknowledges_that_dates_list` | Evening ack carried to morning |
| Kvitterat, sedan ny regelträff tillkommer – bara nya raden visas | `test_packing.py::test_acknowledged_item_is_excluded_but_new_item_still_shown` | |
| Tomt schema respektive HTTP-fel – olika state/kvalitet, aldrig auto-ledig | `test_schedule.py::test_error_and_empty_resolve_differently`, `::test_empty_expected_day_is_no_schedule`, `::test_http_error_on_expected_day_is_source_error`; `test_coordinator.py::test_one_source_error_leaves_others_fine` | |
| Tomt schema lördag vs tisdag – lördag tyst, tisdag "Inget schema registrerat" | `test_schedule.py::test_empty_day_outside_weekday_mask_is_no_activity`, `::test_empty_expected_day_is_no_schedule` | Weekday mask |
| Lämnar hemzonen 06:50, rek. avgång 07:32 – uppdraget förblir aktivt | `test_scheduler.py::test_timeout_tick_evaluates_without_presence` | Presence auto-departure is v1.1; timers carry the mission regardless |
| Morgonnotis 05:45 med tysta tider till 06:00 – skickas 06:00 | `test_notification_policy.py::test_morning_held_inside_quiet_hours`, `::test_morning_skipped_when_departure_too_close_after_quiet` | |
| Vuxen A flexkalender 09:00 – "senast på jobbet 09:00", arrival_minutes 0 | `test_schedule.py::test_adult_arrival_margin_zero_keeps_calendar_time` | |
| SL-buss inställd – ny nåbar resa och relevant varning | `test_planner_transit.py::test_cancelled_leg_rejected_and_fallback_used`; `test_e2e_morning.py::test_cancelled_bus_morning_offers_reachable_alternative` | |
| Andra resbenet försenat, byte missas – hela resan omprövas | `test_planner_transit.py::test_missed_transfer_from_delay_invalidates_journey` | |
| Försenad buss kan återhämta sig – hemavgång skjuts inte osäkert fram | `test_planner_transit.py::test_delayed_bus_does_not_push_departure_later` | |
| Olika turer med samma linjenummer (endast vid separat realtidskälla) | — | **Out of v1 scope.** T00 decided **A** (Journey Planner v2 alone), so there is no separate realtime source to match against (`docs/decisions/sl-realtime.md`) |
| Ingen resa kan ge ankomst i tid – sen ankomst utan falsk latest-on-time | `test_planner_transit.py::test_no_on_time_journey_shows_best_late`, `::test_empty_result_cannot_arrive`; `test_e2e_morning.py::test_no_journey_in_time_shows_late_without_false_on_time` | |
| Waze-fel, tom rutt-lista eller icke-numerisk duration – validering och reservtid | `test_provider_waze.py::test_empty_route_list_is_none`, `::test_non_numeric_duration_is_none`, `::test_service_not_found_is_none`; `test_coordinator.py::test_car_falls_back_to_reserve_minutes`, `::test_car_without_fallback_needs_configuration` | Never travel time = 0 |
| Byte av färdsätt i UI – gammalt svar/timer kan inte återställa föregående plan | `test_coordinator.py::test_today_override_mode_wins`, `::test_stale_revision_round_is_discarded`; `test_entities.py::test_change_today_transport_recomputes` | |
| Små tidsändringar som totalt blir fyra minuter – varning mot senast meddelad tid | `test_notification_policy.py::test_small_changes_summing_past_threshold_alert`, `::test_change_below_threshold_is_silent` | Baseline = last notified leave |
| Omstart kring 10-minutersvarning – ingen dubblett, rimlig catch-up | `test_scheduler.py::test_restart_near_reminder_catches_up_without_duplicate`, `::test_restart_within_window_resends_go_now_if_reachable`, `::test_restart_long_after_leave_now_suppresses_stale_go_now`; `test_e2e_morning.py::test_restart_near_reminder_catches_up_once` | |
| "Jag går nu" från gårdagens notis – ingen effekt på dagens plan | `test_dispatcher.py::test_action_with_wrong_nonce_is_a_noop`, `::test_action_for_unknown_mission_is_a_noop` | Nonce bound to the mission |
| Hemkomst vid lunch – dagens morgonuppdrag förblir avslutat | `test_schedule.py::test_locked_after_departure_keeps_first_event_despite_change`; `test_coordinator.py::test_confirmed_departure_not_reset` | |
| Europe/Stockholm vid tidsomställning – korrekt datum och UTC/local | `test_timeutil.py::test_local_date_of_spring_forward_morning`, `::test_local_date_of_fall_back_morning`, `::test_local_day_bounds_spring_forward_is_23_hours`, `::test_local_day_bounds_fall_back_is_25_hours`, `::test_combine_local_dst_transition_day`; `test_services.py::test_set_override_arrival_time_is_local` | |
| En kanalleverans misslyckas – dashboard och övriga kanaler fortsätter | `test_dispatcher.py::test_one_channel_failing_does_not_stop_the_others`, `::test_event_fired_with_intent_content` | |
| Unload/reload – inga kvarvarande timers eller dubbla listeners | `test_init.py::test_reload_leaves_no_duplicate_listeners`; `test_entities.py::test_entities_unload_cleanly`; `test_scheduler.py::test_shutdown_leaves_no_timers`, `::test_dropped_mission_cancels_its_timers`; `test_e2e_morning.py::test_full_morning_transit_lifecycle` (final tick) | |

## §16 HA-integration tests (prose list)

| Requirement | Test(s) |
| --- | --- |
| Config flow med validering | `test_config_flow.py::test_ics_connection_error_surfaced`, `::test_ics_invalid_body_surfaced`, `::test_ics_source_requires_url`, `::test_calendar_source_requires_entity` |
| Options / reload | `test_config_flow.py::test_options_change_reloads_entry`; `test_init.py::test_reload_leaves_no_duplicate_listeners` |
| Entity registry-stabilitet | `test_entities.py::test_unique_ids_survive_rename` |
| Tjänstevalidering | `test_services.py` (invalid person/date/plan/revision cases) |
| Store-migration och städning | `test_store.py::test_migration_from_v1_payload`, `::test_prune_removes_old_records`, `::test_corrupt_file_yields_empty_state` |
| Providerkontraktstester mot låsta svar | `test_provider_sl.py` (T00 fixtures); `test_provider_ics.py`; `test_provider_ha_calendar.py` |
| Opt-in live smoke test | `test_provider_sl.py::test_live_sl_smoke` (skipped unless `FD_LIVE_SL=1`) |
| Exakt ankomstsökning, tidszon, ID-mappning, realtidskapacitet | `test_provider_sl.py::test_build_trip_params_arrival_contract`, `::test_build_trip_params_local_time_conversion_summer/winter`, `::test_journey_id_is_stable_for_same_trip`, `::test_map_realtime_sets_has_realtime` |

## §16 "Acceptans i verkliga hemmet" (requires the live install, §17 Steg 5)

These are home-acceptance items that need the real sources, phones and routes
(human tasks H2–H7). They are demonstrated structurally in tests but finally
confirmed only on the Green:

| Acceptance item | Structural test | Final confirmation |
| --- | --- | --- |
| Alla fyra profiler konfigureras och visar förklarbara tider | `test_e2e_morning.py::test_all_four_profiles_get_explainable_plans` | Live, §17 Steg 5 |
| Vklass och SchoolSoft med riktiga scheman/filter | `test_provider_ics.py` (sanitised SchoolSoft fixture) | Live (H3, H7) |
| Bil- och SL-planer mot familjens rutter | `test_planner_fixed.py`, `test_planner_transit.py` | Live (H4) |
| Varje telefon får rätt persons provnotis | `test_dispatcher.py::test_test_recipient_reroutes_all_profiles_to_one_script` | Live (H2) |
| Gammal data märkt; API-fel ger aldrig restid noll | `test_coordinator.py::test_car_without_fallback_needs_configuration`; `test_planner_fixed.py::test_stale_duration_marked_stale` | Live |
| Inställd resa demonstreras med fixture utan att störa familjen | `test_e2e_morning.py::test_cancelled_bus_morning_offers_reachable_alternative` | — |
| Dagligt byte av färdsätt utan YAML | `test_entities.py::test_change_today_transport_recomputes` | Live |
| Fem vardagar skuggläge + en vecka med notiser | — | **Human, §17 Steg 5** (shadow mode / tuning) |

## §17 "Klart när" criteria for Steps 1–3

- **Steg 1 (lokal kärna):** all four profiles get a testable plan, ledger/timers
  survive restart, and no phone notifications in dry-run.
  Demonstrated by `test_e2e_morning.py::test_all_four_profiles_get_explainable_plans`
  (plans for all four), `test_scheduler.py::test_restart_near_reminder_catches_up_without_duplicate`
  and `test_e2e_morning.py::test_restart_near_reminder_catches_up_once` (restart
  ledger/timers), and `test_dispatcher.py::test_dry_run_fires_event_but_calls_no_script`
  (no scripts in dry-run).
- **Steg 2 (SL-resor och realtid):** cancellation/transfer cases yield the right
  reachable plan. Demonstrated by the `test_planner_transit.py` cancellation and
  transfer tests and `test_e2e_morning.py::test_cancelled_bus_morning_offers_reachable_alternative`.
  The separate realtime adapter is **not required** (T00 decision A).
- **Steg 3 (bil och individuella notifieringar):** the whole message lifecycle
  works including cleanup and restart, with no duplicates in normal operation.
  Demonstrated end-to-end by `test_e2e_morning.py::test_full_morning_transit_lifecycle`
  (morning → reminder → leave_now → departed → cleanup, single leave_now) and the
  dispatcher/scheduler suites.

The live-only parts of Steg 3 (Live Update rendering on each phone, enabling one
person at a time) and all of §17 Steg 5 (five shadow weekdays, a week of live
notifications, resource checks on the Green) are **human tasks** and are not
covered by automated tests.
