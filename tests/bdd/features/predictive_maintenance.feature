Feature: Predictive maintenance for a fleet manager
  As a fleet manager
  I want to see which vehicles are likely to break down in the next 7 days and act on them
  So that I avoid roadside breakdowns and unplanned downtime

  Background:
    Given I am signed in as the fleet manager of the fleet with the riskiest vehicle

  Scenario: See the riskiest vehicles first
    When I open the at-risk list
    Then I see vehicles of my fleet only
    And they are ordered from highest to lowest 7-day risk
    And each high-risk vehicle explains why it is at risk

  Scenario: Plan the workshop within technician capacity
    When I ask for a workshop plan with 8 technician hours
    Then the plan uses at most 8 hours
    And every planned vehicle is routed to a depot

  Scenario: Copilot proposes a work order that needs my approval
    When I ask the copilot to schedule a service for my riskiest vehicle
    Then a work order is created with status "PROPOSED"
    When I approve that work order
    Then its status becomes "APPROVED"
    And the agent action is recorded in the audit log

  Scenario: I cannot see another fleet's data
    When I request the at-risk list of another fleet
    Then the request is denied with status 403
