Feature: Privacy and compliance
  As a data protection officer
  I want driver data erased on request and every access audited
  So that we comply with GDPR and India's DPDP Act

  Scenario: Right to erasure of a driver
    Given I am signed in as the fleet manager of fleet 1
    When I request erasure of a driver in my fleet
    Then the driver's identity is removed
    And the audit chain still verifies

  Scenario: Analysts only see coarse locations
    Given I am signed in as an analyst of fleet 1
    And a vehicle of fleet 1 has a live position
    When I open that vehicle
    Then its location is rounded to 2 decimal places
