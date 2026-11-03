use crate::{LaunchError, StudioId, StudioInfo};

/// The one Studio present in `after` but not in `before`. Zero or several new ones is an error, never a guess.
pub fn new_studio_id(before: &[StudioInfo], after: &[StudioInfo]) -> Result<StudioId, LaunchError> {
    let mut fresh: Vec<StudioId> = after
        .iter()
        .filter(|studio| !before.iter().any(|known| known.id == studio.id))
        .map(|studio| studio.id.clone())
        .collect();
    match fresh.len() {
        0 => Err(LaunchError::NoNewStudio),
        1 => Ok(fresh.remove(0)),
        _ => Err(LaunchError::Ambiguous(fresh)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn studio(id: &str) -> StudioInfo {
        StudioInfo {
            id: StudioId(id.to_string()),
            name: format!("place {id}"),
        }
    }

    #[test]
    fn one_new_studio_is_returned() {
        let before = [studio("a")];
        let after = [studio("a"), studio("b")];
        assert_eq!(
            new_studio_id(&before, &after).ok(),
            Some(StudioId("b".into()))
        );
    }

    #[test]
    fn no_new_studio_is_an_error() {
        let before = [studio("a")];
        let after = [studio("a")];
        assert!(matches!(
            new_studio_id(&before, &after),
            Err(LaunchError::NoNewStudio)
        ));
    }

    #[test]
    fn two_new_studios_are_ambiguous() {
        let before = [studio("a")];
        let after = [studio("a"), studio("b"), studio("c")];
        match new_studio_id(&before, &after) {
            Err(LaunchError::Ambiguous(ids)) => {
                assert_eq!(ids, vec![StudioId("b".into()), StudioId("c".into())]);
            }
            other => panic!("expected Ambiguous, got {other:?}"),
        }
    }
}
